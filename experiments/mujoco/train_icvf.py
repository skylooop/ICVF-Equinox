import os
import warnings

warnings.filterwarnings("ignore")
os.environ["D4RL_SUPPRESS_IMPORT_ERROR"] = "1"

from absl import app, flags
from functools import partial
import numpy as np
import jax
import jax.numpy as jnp

import equinox as eqx
import tqdm

from src import icvf_learner as learner
from src.icvf_learner import update, eval_ensemble
from src.icvf_networks import icvfs
from icvf_envs.antmaze import d4rl_utils, d4rl_ant, d4rl_pm
from src.gc_dataset import GCSDataset
from src import viz_utils

from jaxrl_m.wandb import setup_wandb, default_wandb_config
import wandb

from ml_collections import config_flags

FLAGS = flags.FLAGS
flags.DEFINE_string('env_name', 'halfcheetah-random-v2', 'Environment name.')

flags.DEFINE_integer('seed', np.random.choice(1000000), 'Random seed.')
flags.DEFINE_integer('log_interval', 1_000, 'Metric logging interval.')
flags.DEFINE_integer('eval_interval', 30_000, 'Visualization interval.')
flags.DEFINE_integer('save_interval', 25_000, 'Save interval.')
flags.DEFINE_integer('batch_size', 256, 'Mini batch size.')
flags.DEFINE_integer('max_steps', int(100_000), 'Number of training steps.')

flags.DEFINE_enum('icvf_type', 'multilinear', list(icvfs), 'Which model to use.')
flags.DEFINE_list('hidden_dims', [256, 256], 'Hidden sizes.')

def update_dict(d, additional):
    d.update(additional)
    return d

wandb_config = update_dict(
    default_wandb_config(),
    {
        'project': 'ICVF_Baseline',
        'group': 'icvf_baseline',
        'name': 'icvf_eqx_{env_name}_Pretraining',
    }
)

config = update_dict(
    learner.get_default_config(),
    {
    'discount': 0.99, 
     'optim_kwargs': { # Standard Adam parameters for non-vision
            'learning_rate': 3e-4,
            'eps': 1e-8
        }
    }
)

gcdataset_config = GCSDataset.get_default_config()

config_flags.DEFINE_config_dict('wandb', wandb_config, lock_config=False)
config_flags.DEFINE_config_dict('config', config, lock_config=False)
config_flags.DEFINE_config_dict('gcdataset', gcdataset_config, lock_config=False)

def main(_):
    # Create wandb logger
    params_dict = {**FLAGS.gcdataset.to_dict(), **FLAGS.config.to_dict()}
    setup_wandb(params_dict, **FLAGS.wandb)
    
    env = d4rl_utils.make_env(FLAGS.env_name)
    dataset = d4rl_utils.get_dataset(env)
    gc_dataset = GCSDataset(dataset, **FLAGS.gcdataset.to_dict())
    example_batch = gc_dataset.sample(1)
    
    hidden_dims = tuple([int(h) for h in FLAGS.hidden_dims])
    agent = learner.create_eqx_learner(FLAGS.seed,
                                       example_batch['observations'],
                                       load_pretrained_icvf=False,
                                       hidden_dims=hidden_dims,
                                       **FLAGS.config)

    example_trajectory = dataset.get_subset(np.arange(0, 100))
    
    for i in tqdm.tqdm(range(1, FLAGS.max_steps + 1),
                       smoothing=0.1,
                       dynamic_ncols=True):
        batch = gc_dataset.sample(FLAGS.batch_size)  
        agent, update_info = update(agent, batch)
        
        if i % FLAGS.log_interval == 0:
            debug_statistics = get_debug_statistics(agent, batch)
            train_metrics = {f'training/{k}': v for k, v in update_info.items()}
            train_metrics.update({f'pretraining/debug/{k}': v for k, v in debug_statistics.items()})
            wandb.log(train_metrics, step=i)
            
        if i % FLAGS.eval_interval == 0:
            visualizations = {}
            traj_metrics = get_traj_v(agent, example_trajectory)
            value_viz = viz_utils.make_visual_no_image(traj_metrics, 
                [
                partial(viz_utils.visualize_metric, metric_name=k) for k in traj_metrics.keys()
                    ]
            )
            visualizations['value_traj_viz'] = wandb.Image(value_viz)
            wandb.log(visualizations)
            
        if i % FLAGS.save_interval == 0:
            unensemble_model = jax.tree_util.tree_map(lambda x: x[0] if eqx.is_array(x) else x, agent.value_learner.model)
            with open("icvf_model_phi.eqx", "wb") as f:
                eqx.tree_serialise_leaves(f, unensemble_model.phi_net)
            with open("icvf_model_psi.eqx", "wb") as f:
                eqx.tree_serialise_leaves(f, unensemble_model.psi_net)
            with open("icvf_model_T.eqx", "wb") as f:
                eqx.tree_serialise_leaves(f, unensemble_model.T_net)
            with open("icvf_model_a.eqx", "wb") as f:
                eqx.tree_serialise_leaves(f, unensemble_model.matrix_a)
            with open("icvf_model_b.eqx", "wb") as f:
                eqx.tree_serialise_leaves(f, unensemble_model.matrix_b)
                

###################################################################################################
#
# Creates wandb plots
#
###################################################################################################
class DebugPlotGenerator:
    def __init__(self, env_name, gc_dataset):
        if 'antmaze' in env_name:
            viz_env, viz_dataset = d4rl_ant.get_env_and_dataset(env_name)
            init_state = np.copy(viz_dataset['observations'][0])
            init_state[:2] = (12.5, 8)
            viz_library = d4rl_ant
            self.viz_things = (viz_env, viz_dataset, viz_library, init_state)

        elif 'maze' in env_name:
            viz_env, viz_dataset = d4rl_pm.get_gcenv_and_dataset(env_name)
            init_state = np.copy(viz_dataset['observations'][0])
            init_state[:2] = (3, 4)
            viz_library = d4rl_pm
            self.viz_things = (viz_env, viz_dataset, viz_library, init_state)
        else:
            raise NotImplementedError('Visualization not implemented for this environment')

    def generate_debug_plots(self, agent):
        example_trajectory = self.example_trajectory
        intents = self.intent_set_batch['observations']
        (viz_env, viz_dataset, viz_library, init_state) = self.viz_things

        visualizations = {}
        traj_metrics = get_traj_v(agent, example_trajectory)
        value_viz = viz_utils.make_visual_no_image(traj_metrics, 
            [
            partial(viz_utils.visualize_metric, metric_name=k) for k in traj_metrics.keys()
                ]
        )
        visualizations['value_traj_viz'] = wandb.Image(value_viz)
    
        if 'maze' in FLAGS.env_name:
            print('Visualizing intent policies and values')
            # Policy visualization
            methods = [
                partial(viz_library.plot_policy, policy_fn=partial(get_policy, agent, intent=intents[idx]))
                for idx in range(9)
            ]
            image = viz_library.make_visual(viz_env, viz_dataset, methods)
            visualizations['intent_policies'] = wandb.Image(image)

            # Value visualization
            methods = [
                partial(viz_library.plot_value, value_fn=partial(get_values, agent, intent=intents[idx]))
                for idx in range(9)
            ]
            image = viz_library.make_visual(viz_env, viz_dataset, methods)
            visualizations['intent_values'] = wandb.Image(image)

            for idx in range(3):
                methods = [
                    partial(viz_library.plot_policy, policy_fn=partial(get_policy, agent, intent=intents[idx])),
                    partial(viz_library.plot_value, value_fn=partial(get_values, agent, intent=intents[idx]))
                ]
                image = viz_library.make_visual(viz_env, viz_dataset, methods)
                visualizations[f'intent{idx}'] = wandb.Image(image)

            image_zz = viz_library.gcvalue_image(
                viz_env,
                viz_dataset,
                partial(get_v_zz, agent),
            )
            image_gz = viz_library.gcvalue_image(
                viz_env,
                viz_dataset,
                partial(get_v_gz, agent, init_state),
            )
            visualizations['v_zz'] = wandb.Image(image_zz)
            visualizations['v_gz'] = wandb.Image(image_gz)
        return visualizations

###################################################################################################
#
# Helper functions for visualization
#
###################################################################################################

@eqx.filter_jit
def get_values(agent, observations, intent):
    def get_v(observations, intent):
        intent = intent.reshape(1, -1)
        intent_tiled = jnp.tile(intent, (observations.shape[0], 1))
        v1, v2 = eval_ensemble(agent.value_learner.model, observations, intent_tiled, intent_tiled)
        return (v1 + v2) / 2    
    return get_v(observations, intent)

@eqx.filter_jit
def get_policy(agent, observations, intent):
    def v(observations):
        def get_v(observations, intent):
            intent = intent.reshape(1, -1)
            intent_tiled = jnp.tile(intent, (observations.shape[0], 1))
            v1, v2 = eval_ensemble(agent.value_learner.model, observations, intent_tiled, intent_tiled)
            return (v1 + v2) / 2    
            
        return get_v(observations, intent).mean()

    grads = eqx.filter_grad(v)(observations)
    policy = grads[:, :2]
    return policy / jnp.linalg.norm(policy, axis=-1, keepdims=True)

@eqx.filter_jit
def get_debug_statistics(agent, batch):
    def get_info(s, g, z):
        if agent.config['no_intent']:
            return agent.value(s, g, jnp.ones_like(z), method='get_info')
        else:
            return eval_ensemble(agent.value_learner.model, s, g, z)
            #return agent.value(s, g, z, method='get_info')

    s = batch['observations']
    g = batch['goals']
    z = batch['desired_goals']

    info_ssz = get_info(s, s, z)
    info_szz = get_info(s, z, z)
    info_sgz = get_info(s, g, z)
    info_sgg = get_info(s, g, g)
    info_szg = get_info(s, z, g)

    if 'phi' in info_sgz:
        stats = {
            'phi_norm': jnp.linalg.norm(info_sgz['phi'], axis=-1).mean(),
            'psi_norm': jnp.linalg.norm(info_sgz['psi'], axis=-1).mean(),
        }
    else:
        stats = {}

    stats.update({
        'v_ssz': info_ssz.mean(),
        'v_szz': info_szz.mean(),
        'v_sgz': info_sgz.mean(),
        'v_sgg': info_sgg.mean(),
        'v_szg': info_szg.mean(),
        'diff_szz_szg': (info_szz - info_szg).mean(),
        'diff_sgg_sgz': (info_sgg - info_sgz).mean(),
    })
    return stats

@eqx.filter_jit
def get_gcvalue(agent, s, g, z):
    v_sgz_1, v_sgz_2 = eval_ensemble(agent.value_learner.model, s, g, z)
    return (v_sgz_1 + v_sgz_2) / 2

def get_v_zz(agent, goal, observations):
    goal = jnp.tile(goal, (observations.shape[0], 1))
    return get_gcvalue(agent, observations, goal, goal)

def get_v_gz(agent, initial_state, target_goal, observations):
    initial_state = jnp.tile(initial_state, (observations.shape[0], 1))
    target_goal = jnp.tile(target_goal, (observations.shape[0], 1))
    return get_gcvalue(agent, initial_state, observations, target_goal)

@eqx.filter_jit
def get_traj_v(agent, trajectory):
    def get_v(s, g):
        return eval_ensemble(agent.value_learner.model, s[None], g[None], g[None]).mean()
    observations = trajectory['observations']
    all_values = jax.vmap(jax.vmap(get_v, in_axes=(None, 0)), in_axes=(0, None))(observations, observations)
    return {
        'dist_to_beginning': all_values[:, 0],
        'dist_to_end': all_values[:, -1],
        'dist_to_middle': all_values[:, all_values.shape[1] // 2],
    }

####################


if __name__ == '__main__':
    app.run(main)

