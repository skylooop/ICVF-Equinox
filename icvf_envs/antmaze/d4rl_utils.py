import d4rl
import gym
import numpy as np

from jaxrl_m.dataset import Dataset
from jaxrl_m.evaluation import EpisodeMonitor

from typing import *

def make_env(env_name: str):
    env = gym.make(env_name)
    env = EpisodeMonitor(env)
    return env

def compute_mean_std(states: np.ndarray, eps: float):
    mean = states.mean(0)
    std = states.std(0) + eps
    return mean, std

def normalize(states, mean, std):
    return (states - mean) / std

def get_dataset(env: gym.Env,
                 clip_to_eps: bool = True,
                 eps: float = 1e-5):
        dataset = d4rl.qlearning_dataset(env)

        if clip_to_eps:
            lim = 1 - eps
            dataset['actions'] = np.clip(dataset['actions'], -lim, lim)

        dones_float = np.zeros_like(dataset['rewards'])

        for i in range(len(dones_float) - 1):
            if np.linalg.norm(dataset['observations'][i + 1] -
                              dataset['next_observations'][i]
                              ) > 1e-6 or dataset['terminals'][i] == 1.0:
                dones_float[i] = 1
            else:
                dones_float[i] = 0

        dones_float[-1] = 1
        custom_flip = np.load("/home/m_bobrin/icvf_release_eqx/cheetah_halfup.npy", allow_pickle=True)
        # dataset['observations'][:5000] = np.tile(custom_flip, reps=(5, 1))
        # dataset['next_observations'][:5000] = np.tile(np.roll(custom_flip, -1), reps=(5, 1))
        dataset['observations'][:1000] = custom_flip
        dataset['next_observations'][:1000] = np.roll(custom_flip, -1, axis=-1)
        return Dataset.create(observations=dataset['observations'].astype(np.float32),
                         actions=dataset['actions'].astype(np.float32),
                         rewards=dataset['rewards'].astype(np.float32),
                         masks=1.0 - dataset['terminals'].astype(np.float32),
                         dones_float=dones_float.astype(np.float32),
                         next_observations=dataset['next_observations'].astype(
                             np.float32),
                         )

def modify_reward(
    dataset: Dict[str, np.ndarray], env_name: str, max_episode_steps: int = 1000
):
    if any(s in env_name.lower() for s in ("halfcheetah", "hopper", "walker2d")):
        dataset['rewards'] = get_normalization(dataset)
    elif "antmaze" in env_name:
        dataset["rewards"] -= 1.0
    return dataset

def get_normalization(dataset):
        returns = []
        ret = 0
        for r, term in zip(dataset['rewards'], dataset['timeouts']):
            ret += r
            if term:
                returns.append(ret)
                ret = 0
        return (max(returns) - min(returns)) / 1000

def normalize_dataset(env_name, dataset):
    if 'antmaze' in env_name:
         return  dataset.copy({'rewards': dataset['rewards']- 1.0})
    else:
        normalizing_factor = get_normalization(dataset)
        dataset = dataset.copy({'rewards': dataset['rewards'] / normalizing_factor})
        return dataset