import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
import pickle
import os
import numpy as np
import abc
from contextlib import contextmanager
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D

class EMA:
    def __init__(self, model, decay):
        self.model = model
        self.decay = decay
        self.shadow = {}
        self.backup = {}

        for name, param in model.named_parameters():
            if param.requires_grad:
                self.shadow[name] = param.data.clone()

    def update(self):
        for name, param in self.model.named_parameters():
            if param.requires_grad:
                assert name in self.shadow
                new_average = (1.0 - self.decay) * param.data + self.decay * self.shadow[name]
                self.shadow[name] = new_average.clone()

    def apply_shadow(self):
        for name, param in self.model.named_parameters():
            if param.requires_grad:
                self.backup[name] = param.data
                param.data = self.shadow[name]

    def restore(self):
        for name, param in self.model.named_parameters():
            if param.requires_grad:
                assert name in self.backup
                param.data = self.backup[name]
        self.backup = {}

class WarmUpScheduler:
    def __init__(self, optimizer, target_lr, warmup_steps):
        self.optimizer = optimizer
        self.target_lr = target_lr
        self.warmup_steps = warmup_steps
        self.step_num = 0

    def step(self):
        self.step_num += 1
        lr = self.target_lr * min(1.0, self.step_num / self.warmup_steps)
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr

def save_model(model, ema_model, epoch, loss, model_name, checkpoint_dir, best_checkpoints):
    def write_model(model, path, epoch, loss, is_ema=False):
        state_dict = model.state_dict() if not is_ema else ema_model.shadow
        torch.save({
            'epoch': epoch,
            'model_state_dict': state_dict,
            'loss': loss,
        }, path)
    
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    
    last_checkpoint_path = os.path.join(checkpoint_dir, f"{model_name}_last.pth")
    write_model(model, last_checkpoint_path, epoch, loss)

    last_ema_checkpoint_path = os.path.join(checkpoint_dir, f"{model_name}_last_EMA.pth")
    write_model(model, last_ema_checkpoint_path, epoch, loss, is_ema=True)
    
    if len(best_checkpoints) < 3:
        new_checkpoint_path = os.path.join(checkpoint_dir, f"{model_name}_epoch_{epoch}_loss_{loss:.3f}.pth")
        new_ema_checkpoint_path = os.path.join(checkpoint_dir, f"{model_name}_epoch_{epoch}_loss_{loss:.3f}_EMA.pth")
        best_checkpoints.append((new_checkpoint_path, new_ema_checkpoint_path, loss))
        write_model(model, new_checkpoint_path, epoch, loss)
        write_model(model, new_ema_checkpoint_path, epoch, loss, is_ema=True)
    else:
        worst_checkpoint = max(best_checkpoints, key=lambda x: x[2])
        if loss < worst_checkpoint[2]:
            best_checkpoints.remove(worst_checkpoint)
            os.remove(worst_checkpoint[0])
            os.remove(worst_checkpoint[1])

            new_checkpoint_path = os.path.join(checkpoint_dir, f"{model_name}_epoch_{epoch}_loss_{loss:.3f}.pth")
            new_ema_checkpoint_path = os.path.join(checkpoint_dir, f"{model_name}_epoch_{epoch}_loss_{loss:.3f}_EMA.pth")
            best_checkpoints.append((new_checkpoint_path, new_ema_checkpoint_path, loss))
            
            write_model(model, new_checkpoint_path, epoch, loss)
            write_model(model, new_ema_checkpoint_path, epoch, loss, is_ema=True)
    
            print(f"{model_name} model saved at '{new_checkpoint_path}'")
            print(f"{model_name} EMA model saved at '{new_ema_checkpoint_path}'")



def load_model(model, ema_model, checkpoint_path, model_name, is_ema=False):
    checkpoint = torch.load(checkpoint_path)
    if is_ema:
        for name, data in checkpoint['model_state_dict'].items():
            ema_model.shadow[name].copy_(data)
    else:
        model.load_state_dict(checkpoint['model_state_dict'])
    epoch = checkpoint['epoch']
    loss = checkpoint['loss']
    print(f"{model_name} {'EMA' if is_ema else ''} model loaded from '{checkpoint_path}', Epoch: {epoch}, Loss: {loss}")
    return epoch, loss


def get_score_fn(sde, diffusion_model):
    def score_fn(x, t):
        noise_prediction = diffusion_model(x, t)
        _, std = sde.marginal_prob(x, t)
        std = std.view(std.shape[0], *[1 for _ in range(len(x.shape) - 1)])  # Expand std to match the shape of noise_prediction
        score = -noise_prediction / std
        return score
    return score_fn

def eval_callback(score_fn, sde, val_dataloader, num_datapoints, device, save_path, name=None, return_svd=False):
    os.makedirs(save_path, exist_ok=True)

    singular_values = []
    idx = 0
    sampling_eps = sde.sampling_eps

    with tqdm(total=num_datapoints) as pbar:
        for x in val_dataloader:
            print(len(x))
            orig_batch = x[0]
            orig_batch = orig_batch.to(device)
            batchsize = orig_batch.size(0)

            if idx >= num_datapoints:
                break

            for x in orig_batch:
                if idx >= num_datapoints:
                    break

                ambient_dim = np.prod(x.shape[1:])
                x = x.repeat([batchsize] + [1 for _ in range(len(x.shape))])
                print("Repeated x shape:", x.shape)

                num_batches = int(np.floor(ambient_dim / batchsize)) + 1
                num_batches *= 2

                t = sampling_eps
                vec_t = torch.ones(x.size(0), device=device) * t

                scores = []
                for i in range(1, num_batches + 1):
                    batch = x.clone()

                    mean, std = sde.marginal_prob(batch, vec_t)
                    z = torch.randn_like(batch)
                    batch = mean + std[(...,) + (None,) * len(batch.shape[1:])] * z
                    print(batch.size(), vec_t.size())
                    score = score_fn(batch, vec_t).detach().cpu()
                    print("Score shape:", score.shape)
                    scores.append(score)

                scores = torch.cat(scores, dim=0)
                print(score.size())
                scores = torch.flatten(scores, start_dim=1)
                print("Flattened scores shape:", scores.shape)

                means = scores.mean(dim=0, keepdim=True)
                normalized_scores = scores - means

                u, s, v = torch.linalg.svd(normalized_scores)
                singular_values.append(s.tolist())
                print("Singular values shape:", s.shape)

                idx += 1
                pbar.update(1)

    info = {'singular_values': singular_values}
    if return_svd:
        return info
    else:
        if name is None:
            name = 'svd'
        with open(os.path.join(save_path, f'{name}.pkl'), 'wb') as f:
            pickle.dump(info, f)
