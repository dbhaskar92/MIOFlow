# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/05_train.ipynb.

# %% auto 0
__all__ = ['DEBUG', 'subset_threshold', 'train', 'train_ae', 'training_regimen']

# %% ../nbs/05_train.ipynb 3
import torch
def subset_threshold(m_tp, x_tp, threshold_factor=0.1):
    # this time m_tp and x_tp are not full seq but just one time point.
    if threshold_factor is None:
        return m_tp, x_tp
    threshold = threshold_factor * m_tp.mean(dim=-1, keepdim=True)
    mask = m_tp > threshold
    return m_tp[mask], x_tp[mask]

# %% ../nbs/05_train.ipynb 4
import os, sys, json, math, itertools
import pandas as pd, numpy as np
import warnings

# from tqdm import tqdm
from tqdm.notebook import tqdm

import torch

from .utils import sample, generate_steps
from .losses import MMD_loss, OT_loss, Density_loss, Local_density_loss, density_specified_OT_loss, EnergyLoss, EnergyLossGrowthRate, EnergyLossSeq, EnergyLossGrowthRateSeq
from .models import GrowthRateModel, GrowthRateSDEModel

DEBUG = False
def train(
    model, df, groups, optimizer, n_batches=20, 
    criterion=MMD_loss(),
    use_cuda=False,

    sample_size=(100, ),
    sample_with_replacement=False,

    local_loss=True,
    global_loss=False,

    hold_one_out=False,
    hold_out='random',
    apply_losses_in_time=True,

    top_k = 5,
    hinge_value = 0.01,
    use_density_loss=True,
    density_detach_m = True,
    # use_local_density=False,

    lambda_density = 1.0,

    autoencoder=None, 
    use_emb=True,
    use_gae=False,

    use_gaussian:bool=True, 
    add_noise:bool=False, 
    noise_scale:float=0.1,
    
    logger=None,

    use_penalty=False,
    lambda_energy=1.0,

    reverse:bool = False,
    lambda_m = 0.,
    lambda_m2 = 0.,

    use_penalty_m = False,

    lambda_energy_m = 1.0,
    lambda_ot = 1.0,
    energy_weighted=True,
    energy_detach_m=False,

    clip_grad=False,
    clip_grad_norm=1.0,
    threshold_factor=0.1,
    detach_x=False,
    detach_m=False,

    # regularizations for diffusion term
    diffusion_lambda_energy=1.0,
    diffusion_lambda_energy_m=1.0,
    diffusion_energy_weighted=True,
    diffusion_energy_detach_m=False,
):

    '''
    MIOFlow training loop
    
    Notes:
        - The argument `model` must have a method `forward` that accepts two arguments
            in its function signature:
                ```python
                model.forward(x, t)
                ```
            where, `x` is the input tensor and `t` is a `torch.Tensor` of time points (float).
        - The training loop is divided in two parts; local (predict t+1 from t), and global (predict the entire trajectory).
                        
    Arguments:
        model (nn.Module): the initialized pytorch ODE model.
        
        df (pd.DataFrame): the DataFrame from which to extract batch data.
        
        groups (list): the list of the numerical groups in the data, e.g. 
            `[1.0, 2.0, 3.0, 4.0, 5.0]`, if the data has five groups.
    
        optimizer (torch.optim): an optimizer initilized with the model's parameters.
        
        n_batches (int): Default to '20', the number of batches from which to randomly sample each consecutive pair
            of groups.
            
        criterion (Callable | nn.Loss): a loss function.
        
        use_cuda (bool): Defaults to `False`. Whether or not to send the model and data to cuda. 

        sample_size (tuple): Defaults to `(100, )`

        sample_with_replacement (bool): Defaults to `False`. Whether or not to sample data points with replacement.
        
        local_loss (bool): Defaults to `True`. Whether or not to use a local loss in the model.
            See notes for more detail.
            
        global_loss (bool): Defaults to `False`. Whether or not to use a global loss in the model.
        
        hold_one_out (bool): Defaults to `False`. Whether or not to randomly hold one time pair
            e.g. t_1 to t_2 out when computing the global loss.

        hold_out (str | int): Defaults to `"random"`. Which time point to hold out when calculating the
            global loss.
            
        apply_losses_in_time (bool): Defaults to `True`. Applies the losses and does back propegation
            as soon as a loss is calculated. See notes for more detail.

        top_k (int): Default to '5'. The k for the k-NN used in the density loss.

        hinge_value (float): Defaults to `0.01`. The hinge value for density loss.

        use_density_loss (bool): Defaults to `True`. Whether or not to add density regularization.

        lambda_density (float): Defaults to `1.0`. The weight for density loss.

        autoencoder (NoneType|nn.Module): Default to 'None'. The full geodesic Autoencoder.

        use_emb (bool): Defaults to `True`. Whether or not to use the embedding model.
        
        use_gae (bool): Defaults to `False`. Whether or not to use the full Geodesic AutoEncoder.

        use_gaussian (bool): Defaults to `True`. Whether to use random or gaussian noise.

        add_noise (bool): Defaults to `False`. Whether or not to add noise.

        noise_scale (float): Defaults to `0.30`. How much to scale the noise by.
        
        logger (NoneType|Logger): Default to 'None'. The logger to record information.

        use_penalty (bool): Defaults to `False`. Whether or not to use $L_e$ during training (norm of the derivative).
        
        lambda_energy (float): Default to '1.0'. The weight of the energy penalty.

        reverse (bool): Whether to train time backwards.
    '''

    """
    Xingzhi: changed the energy penalty to being computed outside the model.
    """
    if autoencoder is None and (use_emb or use_gae):
        use_emb = False
        use_gae = False
        warnings.warn('\'autoencoder\' is \'None\', but \'use_emb\' or \'use_gae\' is True, both will be set to False.')

    noise_fn = torch.randn if use_gaussian else torch.rand
    def noise(data):
        return noise_fn(*data.shape).cuda() if use_cuda else noise_fn(*data.shape)
    # Create the indicies for the steps that should be used
    steps = generate_steps(groups)

    if reverse:
        groups = groups[::-1]
        steps = generate_steps(groups)

    
    # Storage variables for losses
    batch_losses = []
    globe_losses = []
    if hold_one_out and hold_out in groups:
        groups_ho = [g for g in groups if g != hold_out]
        local_losses = {f'{t0}:{t1}':[] for (t0, t1) in generate_steps(groups_ho) if hold_out not in [t0, t1]}
    else:
        local_losses = {f'{t0}:{t1}':[] for (t0, t1) in steps}
        
    density_fn = Density_loss(hinge_value) # if not use_local_density else Local_density_loss()
    # Send model to cuda and specify it as training mode
    if use_cuda:
        model = model.cuda()
    
    if isinstance(model, GrowthRateModel) or isinstance(model, GrowthRateSDEModel):
        growth_rate = True
        assert isinstance(criterion, density_specified_OT_loss), 'Criterion must be density_specified_OT_loss when using growth_rate=True'
    else:
        growth_rate = False

    # if use_penalty:
    #     model.use_norm = True
    
    # if use_penalty_m:
    #     model.use_norm_m = True

    energy_loss_growth_rate = EnergyLossGrowthRate(weighted=energy_weighted, detach_m=energy_detach_m)
    energy_loss_growth_rate_seq = EnergyLossGrowthRateSeq(weighted=energy_weighted, detach_m=energy_detach_m)
    energy_loss_sde_growth_rate = EnergyLossGrowthRate(weighted=diffusion_energy_weighted, detach_m=diffusion_energy_detach_m)
    energy_loss_sde_growth_rate_seq = EnergyLossGrowthRateSeq(weighted=diffusion_energy_weighted, detach_m=diffusion_energy_detach_m)
    energy_loss = EnergyLoss()
    energy_loss_seq = EnergyLossSeq()

    assert use_penalty == False and use_penalty_m == False, 'Use energy penalty instead of norm penalty!'

    model.train()
    
    for batch in tqdm(range(n_batches)):
        # print(f'Batch {batch+1}/{n_batches}')
        # apply local loss
        if local_loss and not global_loss:
            # for storing the local loss with calling `.item()` so `loss.backward()` can still be used
            batch_loss = []
            if hold_one_out:
                groups = [g for g in groups if g != hold_out] # TODO: Currently does not work if hold_out='random'. Do to_ignore before. 
                steps = generate_steps(groups)
            for step_idx, (t0, t1) in enumerate(steps):  
                if hold_out in [t0, t1] and hold_one_out: # TODO: This `if` can be deleted since the groups does not include the ho timepoint anymore
                    continue                              # i.e. it is always False. 
                optimizer.zero_grad()
                
                #sampling, predicting, and evaluating the loss.
                # sample data
                data_t0 = sample(df, t0, size=sample_size, replace=sample_with_replacement, to_torch=True, use_cuda=use_cuda)
                data_t1 = sample(df, t1, size=sample_size, replace=sample_with_replacement, to_torch=True, use_cuda=use_cuda)
                time = torch.Tensor([t0, t1]).cuda() if use_cuda else torch.Tensor([t0, t1])
                if add_noise:
                    data_t0 += noise(data_t0) * noise_scale
                    data_t1 += noise(data_t1) * noise_scale
                if autoencoder is not None and use_gae:
                    data_t0 = autoencoder.encoder(data_t0)
                    data_t1 = autoencoder.encoder(data_t1)
                # prediction
                if growth_rate:
                    data_tp0, m_tp0 = model(data_t0, time)
                    # if m_tp.min() <= 0.:
                    #     print('m:', m_tp.detach().cpu().numpy().tolist())
                    #     for name, param in model.named_parameters():
                    #         if param.requires_grad:
                    #             print(f"Gradient for {name}: {param.grad.max()}")
                    if detach_x:
                        data_tp = data_tp0.detach()
                    else:
                        data_tp = data_tp0
                    if detach_m:
                        m_tp = m_tp0.detach()
                    else:
                        m_tp = m_tp0
                else:
                    data_tp = model(data_t0, time)

                if autoencoder is not None and use_emb:        
                    data_tp, data_t1 = autoencoder.encoder(data_tp), autoencoder.encoder(data_t1)
                # loss between prediction and sample t1
                if growth_rate:
                    m_tp_threshold, data_tp_threshold = subset_threshold(m_tp, data_tp, threshold_factor=threshold_factor)
                    loss = lambda_ot * criterion(data_tp_threshold, data_t1, m_tp_threshold)
                    # loss = lambda_ot * criterion(data_tp, data_t1, m_tp)

                    # if loss.isnan().any():
                    #     print(m_tp)
                    #     print(data_tp)
                    if DEBUG and m_tp.min() <= 0.:
                        print("OT loss", loss.item())
                else:
                    loss = lambda_ot * criterion(data_tp, data_t1)

                if use_density_loss: 
                    if growth_rate:
                        if density_detach_m:
                            m_tp_threshold, data_tp_threshold = subset_threshold(m_tp.detach(), data_tp, threshold_factor=threshold_factor)
                            density_loss = density_fn(data_tp_threshold, data_t1, pre_softmax_weights=m_tp_threshold, top_k=top_k)
                        else:
                            m_tp_threshold, data_tp_threshold = subset_threshold(m_tp, data_tp, threshold_factor=threshold_factor)
                            density_loss = density_fn(data_tp_threshold, data_t1, pre_softmax_weights=m_tp_threshold, top_k=top_k)
                        if DEBUG and m_tp.min() <= 0.:
                            print("Density loss", density_loss.item())
                    else:               
                        density_loss = density_fn(data_tp, data_t1, top_k=top_k)
                    density_loss = density_loss.to(loss.device)
                    loss += lambda_density * density_loss

                # if use_penalty:
                #     if growth_rate:
                #         NotImplemented
                #     else:
                #         dxdx = model.func(time, data_tp)
                #     penalty = sum(model.norm)
                #     loss += lambda_energy * penalty
                
                # if use_penalty_m:
                #     penalty_m = sum(model.norm_m)
                #     loss += lambda_energy_m * penalty_m
                
                if growth_rate and (lambda_energy > 0 or lambda_energy_m > 0):
                    eloss, emloss = energy_loss_growth_rate(model.func, data_tp, m_tp, time[-1])
                    if DEBUG and m_tp.min() <= 0.:
                        print("Energy loss", eloss.item())
                        print("Energy mass loss", emloss.item())
                    loss += lambda_energy * eloss
                    loss += lambda_energy_m * emloss
                elif lambda_energy > 0:
                    eloss = energy_loss(model.func, data_tp, time[-1])
                    loss += lambda_energy * eloss
                # penalize diffusion term.
                if isinstance(model, GrowthRateSDEModel) and (diffusion_lambda_energy > 0 or diffusion_lambda_energy_m > 0):
                    eloss, emloss = energy_loss_sde_growth_rate(model.gunc, data_tp, m_tp, time[-1])
                    if DEBUG and m_tp.min() <= 0.:
                        print("Diffusion Energy loss", eloss.item())
                        print("Diffusion Energy mass loss", emloss.item())
                    loss += diffusion_lambda_energy * eloss
                    loss += diffusion_lambda_energy_m * emloss

                if growth_rate and lambda_m > 0:
                    # now taking the mean over all points, 
                    # because we allow individual points to be large or small in mass,
                    # but we want the average to stay the same for stablity.
                    m_loss = (torch.square(m_tp.mean(axis=-1) - model.m_init)).mean() 
                    if DEBUG and m_tp.min() <= 0.:
                        print("Mass loss", m_loss.item())
                    loss += lambda_m * m_loss
                if growth_rate and lambda_m2 > 0:
                    m_loss = (torch.square(m_tp - model.m_init)).mean()
                    if DEBUG and m_tp.min() <= 0.:
                        print("Mass loss 2", m_loss.item())
                    loss += lambda_m2 * m_loss

                # apply local loss as we calculate it
                if apply_losses_in_time and local_loss:
                    loss.backward()
                    if DEBUG and m_tp.min() <= 0.:
                        print('m:', m_tp.detach().cpu().numpy().tolist())
                        print('x:', data_tp.detach().cpu().numpy().tolist())
                        for name, param in model.named_parameters():
                            if param.requires_grad:
                                print(f"Gradient for {name}: {param.grad.max()}")
                    if clip_grad:
                        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=clip_grad_norm)
                        if DEBUG and m_tp.min() <= 0.:
                            for name, param in model.named_parameters():
                                if param.requires_grad:
                                    print(f"After clipping: Gradient for {name}: {param.grad.max()}")
                    optimizer.step()
                    # model.norm=[]
                    # model.norm_m=[]
                # save loss in storage variables 
                local_losses[f'{t0}:{t1}'].append(loss.item())
                batch_loss.append(loss)
        
        
            # convert the local losses into a tensor of len(steps)
            batch_loss = torch.Tensor(batch_loss).float()
            if use_cuda:
                batch_loss = batch_loss.cuda()
            
            if not apply_losses_in_time:
                batch_loss.backward()
                if clip_grad:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=clip_grad_norm)
                optimizer.step()

            # store average / sum of local losses for training
            ave_local_loss = torch.mean(batch_loss)
            sum_local_loss = torch.sum(batch_loss)            
            batch_losses.append(ave_local_loss.item())
        
        # apply global loss
        elif global_loss and not local_loss:
            optimizer.zero_grad()
            #sampling, predicting, and evaluating the loss.
            # sample data
            data_ti = [
                sample(
                    df, group, size=sample_size, replace=sample_with_replacement, 
                    to_torch=True, use_cuda=use_cuda
                )
                for group in groups
            ]
            time = torch.Tensor(groups).cuda() if use_cuda else torch.Tensor(groups)

            if add_noise:
                data_ti = [
                    data + noise(data) * noise_scale for data in data_ti
                ]
            if autoencoder is not None and use_gae:
                data_ti = [autoencoder.encoder(data) for data in data_ti]
            # prediction
            if growth_rate:
                data_tp0, m_tp0 = model(data_ti[0], time, return_whole_sequence=True)
                if detach_x:
                    data_tp = data_tp0.detach()
                else:
                    data_tp = data_tp0
                if detach_m:
                    m_tp = m_tp0.detach()
                else:
                    m_tp = m_tp0
            else:
                data_tp = model(data_ti[0], time, return_whole_sequence=True)
            if autoencoder is not None and use_emb:        
                data_tp = [autoencoder.encoder(data) for data in data_tp]
                data_ti = [autoencoder.encoder(data) for data in data_ti]

            #ignoring one time point
            to_ignore = None #TODO: This assignment of `to_ingnore`, could be moved at the beginning of the function. 
            if hold_one_out and hold_out == 'random':
                to_ignore = np.random.choice(groups)
            elif hold_one_out and hold_out in groups:
                to_ignore = hold_out
            elif hold_one_out:
                raise ValueError('Unknown group to hold out')
            else:
                pass

            non_ignore_idx = [i for i in range(len(groups)) if groups[i] != to_ignore]

            # print("non_ignore_idx", non_ignore_idx)

            if growth_rate:
                ot_loss = 0.
                for i in range(1, len(groups)):
                    if groups[i] != to_ignore:
                        mi_sub, xi_sub = subset_threshold(m_tp[i], data_tp[i], threshold_factor=threshold_factor)
                        ot_loss += criterion(
                            xi_sub,
                            data_ti[i],
                            mi_sub
                        )
                loss = lambda_ot * ot_loss
            else:
                loss = sum([
                    criterion(data_tp[i], data_ti[i]) 
                    for i in range(1, len(groups))
                    if groups[i] != to_ignore
                ])
                loss = lambda_ot * loss

            if use_density_loss:                
                if growth_rate:
                    if density_detach_m:
                        m_tp_threshold, data_tp_threshold = subset_threshold(m_tp.detach(), data_tp, threshold_factor=threshold_factor)
                        density_loss = density_fn(data_tp_threshold, data_ti, groups, to_ignore, top_k, pre_softmax_weights=m_tp_threshold)
                    else:
                        m_tp_threshold, data_tp_threshold = subset_threshold(m_tp, data_tp, threshold_factor=threshold_factor)
                        density_loss = density_fn(data_tp_threshold, data_ti, groups, to_ignore, top_k, pre_softmax_weights=m_tp_threshold)
                else:               
                    density_loss = density_fn(data_tp, data_ti, groups, to_ignore, top_k)
                density_loss = density_loss.to(loss.device)
                loss += lambda_density * density_loss

            if growth_rate and (lambda_energy > 0 or lambda_energy_m > 0):
                eloss, emloss = energy_loss_growth_rate_seq(model.func, data_tp[non_ignore_idx,...], m_tp[non_ignore_idx,...], time[non_ignore_idx,...])
                loss += lambda_energy * eloss
                loss += lambda_energy_m * emloss
            elif lambda_energy > 0:
                eloss = energy_loss_seq(model.func, data_tp[non_ignore_idx,...], time[non_ignore_idx,...])
                loss += lambda_energy * eloss
            # penalize diffusion term.
            if isinstance(model, GrowthRateSDEModel) and (diffusion_lambda_energy > 0 or diffusion_lambda_energy_m > 0):
                eloss, emloss = energy_loss_sde_growth_rate_seq(model.gunc, data_tp[non_ignore_idx,...], m_tp[non_ignore_idx,...], time[non_ignore_idx,...])
                loss += diffusion_lambda_energy * eloss
                loss += diffusion_lambda_energy_m * emloss

            # if use_penalty:
            #     penalty = sum([model.norm[-(i+1)] for i in range(1, len(groups))
            #         if groups[i] != to_ignore])
            #     loss += lambda_energy * penalty

            # if use_penalty_m:
            #     penalty_m = sum([model.norm_m[-(i+1)] for i in range(1, len(groups))
            #         if groups[i] != to_ignore])
            #     loss += lambda_energy_m * penalty_m

            if growth_rate and lambda_m > 0:
                # now taking the mean over all points, 
                # because we allow individual points to be large or small in mass,
                # but we want the average to stay the same for stablity.
                m_loss = (torch.square(m_tp[non_ignore_idx,...].mean(axis=-1) - model.m_init)).mean() 
                loss += lambda_m * m_loss
            if growth_rate and lambda_m2 > 0:
                m_loss = (torch.square(m_tp - model.m_init)).mean()
                loss += lambda_m2 * m_loss

                                       
            loss.backward()
            if clip_grad:
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=clip_grad_norm)
            optimizer.step()
            model.norm=[]
            model.norm_m=[]

            globe_losses.append(loss.item())
        elif local_loss and global_loss:
            # NOTE: weighted local / global loss has been removed to improve runtime
            raise NotImplementedError()
        else:
            raise ValueError('A form of loss must be specified.')
        # Check for NaN loss
        if torch.isnan(loss):
            raise ValueError(f"NaN loss encountered at batch {batch}. Stopping training.")
             
    print_loss = globe_losses if global_loss else batch_losses 
    if logger is None:      
        tqdm.write(f'Train loss: {np.round(np.mean(print_loss), 5)}')
    else:
        logger.info(f'Train loss: {np.round(np.mean(print_loss), 5)}')
    return local_losses, batch_losses, globe_losses

# %% ../nbs/05_train.ipynb 5
from .utils import generate_steps
import torch.nn as nn
from tqdm.notebook import tqdm
import numpy as np

def train_ae(
    model, df, groups, optimizer,
    n_epochs=60, criterion=nn.MSELoss(), dist=None, recon = True,
    use_cuda=False, sample_size=(100, ),
    sample_with_replacement=False,
    noise_min_scale=0.09,
    noise_max_scale=0.15,
    hold_one_out:bool=False,
    hold_out='random'
    
):
    """
    Geodesic Autoencoder training loop.
    
    Notes:
        - We can train only the encoder the fit the geodesic distance (recon=False), or the full geodesic Autoencoder (recon=True),
            i.e. matching the distance and reconstruction of the inputs.
            
    Arguments:
    
        model (nn.Module): the initialized pytorch Geodesic Autoencoder model.

        df (pd.DataFrame): the DataFrame from which to extract batch data.
        
        groups (list): the list of the numerical groups in the data, e.g. 
            `[1.0, 2.0, 3.0, 4.0, 5.0]`, if the data has five groups.

        optimizer (torch.optim): an optimizer initilized with the model's parameters.

        n_epochs (int): Default to '60'. The number of training epochs.

        criterion (torch.nn). Default to 'nn.MSELoss()'. The criterion to minimize. 

        dist (NoneType|Class). Default to 'None'. The distance Class with a 'fit(X)' method for a dataset 'X'. Computes the pairwise distances in 'X'.

        recon (bool): Default to 'True'. Whether or not the apply the reconstruction loss. 
        
        use_cuda (bool): Defaults to `False`. Whether or not to send the model and data to cuda. 
        
        sample_size (tuple): Defaults to `(100, )`.
        
        sample_with_replacement (bool): Defaults to `False`. Whether or not to sample data points with replacement.
        
        noise_min_scale (float): Default to '0.0'. The minimum noise scale. 
        
        noise_max_scale (float): Default to '1.0'. The maximum noise scale. The true scale is sampled between these two bounds for each epoch. 
        
        hold_one_out (bool): Default to False, whether or not to ignore a timepoint during training.
        
        hold_out (str|int): Default to 'random', the timepoint to hold out, either a specific element of 'groups' or a random one. 
    
    """
    steps = generate_steps(groups)
    losses = []

    model.train()
    for epoch in tqdm(range(n_epochs)):
        
        # ignoring one time point
        to_ignore = None
        if hold_one_out and hold_out == 'random':
            to_ignore = np.random.choice(groups)
        elif hold_one_out and hold_out in groups:
            to_ignore = hold_out
        elif hold_one_out:
            raise ValueError('Unknown group to hold out')
        else:
            pass
        
        # Training
        optimizer.zero_grad()
        noise_scale = torch.FloatTensor(1).uniform_(noise_min_scale, noise_max_scale)
        data_ti = torch.vstack([sample(df, group, size=sample_size, replace=sample_with_replacement, to_torch=True, use_cuda=use_cuda) for group in groups if group != to_ignore])
        noise = (noise_scale*torch.randn(data_ti.size())).cuda() if use_cuda else noise_scale*torch.randn(data_ti.size())
        
        encode_dt = model.encoder(data_ti + noise)
        recon_dt = model.decoder(encode_dt) if recon else None
        
        if recon:
            loss_recon = criterion(recon_dt,data_ti)
            loss = loss_recon
            
            if epoch%50==0:
                tqdm.write(f'Train loss recon: {np.round(np.mean(loss_recon.item()), 5)}')
        
        if dist is not None:
            dist_geo = dist.fit(data_ti.cpu().numpy())
            dist_geo = torch.from_numpy(dist_geo).float().cuda() if use_cuda else torch.from_numpy(dist_geo).float()
            dist_emb = torch.cdist(encode_dt,encode_dt)**2
            loss_dist = criterion(dist_emb,dist_geo)
            loss = loss_recon + loss_dist if recon else loss_dist
            
            if epoch%50==0:
                tqdm.write(f'Train loss dist: {np.round(np.mean(loss_dist.item()), 5)}')
                
        loss.backward()
        optimizer.step()
        
        losses.append(loss.item())
    return losses

# %% ../nbs/05_train.ipynb 6
from .plots import plot_comparision, plot_losses
from .eval import generate_plot_data

def training_regimen(
    n_local_epochs, n_epochs, n_post_local_epochs,
    exp_dir, 

    # BEGIN: train params
    model, df, groups, optimizer, n_batches=20, 
    criterion=MMD_loss(), use_cuda=False,


    hold_one_out=False, hold_out='random', 
    hinge_value=0.01, use_density_loss=True, density_detach_m=True,

    top_k = 5, lambda_density = 1.0, 
    autoencoder=None, use_emb=True, use_gae=False, 
    sample_size=(100, ), 
    sample_with_replacement=False, 
    logger=None, 
    add_noise=False, noise_scale=0.1, use_gaussian=True,  
    use_penalty=False, lambda_energy=1.0,
    # END: train params



    steps=None, plot_every=None,
    n_points=100, n_trajectories=100, n_bins=100, 
    local_losses=None, batch_losses=None, globe_losses=None,
    reverse_schema=True, reverse_n=4,

    # additional train params. appeneded instead of inserted in case some code did not specify parameter names.
    lambda_m = 0.,
    lambda_m2 = 0.,
    use_penalty_m = False,
    lambda_energy_m = 1.0,
    energy_weighted=True,
    energy_detach_m=False,
    lambda_ot = 1.0,
    threshold_factor=0.1,
    detach_x=False,
    detach_m=False,

    # regularizations for diffusion term
    diffusion_lambda_energy=1.0,
    diffusion_lambda_energy_m=1.0,
    diffusion_energy_weighted=True,
    diffusion_energy_detach_m=False,
):
    recon = use_gae and not use_emb
    if steps is None:
        steps = generate_steps(groups)
        
    if local_losses is None:
        if hold_one_out and hold_out in groups:
            groups_ho = [g for g in groups if g != hold_out]
            local_losses = {f'{t0}:{t1}':[] for (t0, t1) in generate_steps(groups_ho) if hold_out not in [t0, t1]}
            if reverse_schema:
                local_losses = {
                    **local_losses, 
                    **{f'{t0}:{t1}':[] for (t0, t1) in generate_steps(groups_ho[::-1]) if hold_out not in [t0, t1]}
                }
        else:
            local_losses = {f'{t0}:{t1}':[] for (t0, t1) in generate_steps(groups)}
            if reverse_schema:
                local_losses = {
                    **local_losses, 
                    **{f'{t0}:{t1}':[] for (t0, t1) in generate_steps(groups[::-1])}
                }
    if batch_losses is None:
        batch_losses = []
    if globe_losses is None:
        globe_losses = []
    
    reverse = False
    for epoch in tqdm(range(n_local_epochs), desc='Pretraining Epoch'):
        reverse = True if reverse_schema and epoch % reverse_n == 0 else False

        l_loss, b_loss, g_loss = train(
            model, df, groups, optimizer, n_batches, 
            criterion = criterion, use_cuda = use_cuda,
            local_loss=True, global_loss=False, apply_losses_in_time=True,
            hold_one_out=hold_one_out, hold_out=hold_out, 
            hinge_value=hinge_value,
            use_density_loss = use_density_loss,    
            top_k = top_k, lambda_density = lambda_density, density_detach_m=density_detach_m,
            autoencoder = autoencoder, use_emb = use_emb, use_gae = use_gae, sample_size=sample_size, 
            sample_with_replacement=sample_with_replacement, logger=logger,
            add_noise=add_noise, noise_scale=noise_scale, use_gaussian=use_gaussian, 
            use_penalty=use_penalty, lambda_energy=lambda_energy, reverse=reverse,
            lambda_m=lambda_m, lambda_m2=lambda_m2,
            use_penalty_m=use_penalty_m, lambda_energy_m=lambda_energy_m,
            lambda_ot=lambda_ot,
            energy_weighted=energy_weighted,
            energy_detach_m=energy_detach_m,
            threshold_factor=threshold_factor,
            detach_x=detach_x,
            detach_m=detach_m,
            diffusion_lambda_energy=diffusion_lambda_energy,
            diffusion_lambda_energy_m=diffusion_lambda_energy_m, 
            diffusion_energy_weighted=diffusion_energy_weighted,
            diffusion_energy_detach_m=diffusion_energy_detach_m,
        )
        for k, v in l_loss.items():  
            local_losses[k].extend(v)
        batch_losses.extend(b_loss)
        globe_losses.extend(g_loss)
        if plot_every is not None and epoch % plot_every == 0:
            generated, trajectories = generate_plot_data(
                model, df, n_points, n_trajectories, n_bins, 
                sample_with_replacement=sample_with_replacement, use_cuda=use_cuda, 
                samples_key='samples', logger=logger,
                autoencoder=autoencoder, recon=recon
            )
            plot_comparision(
                df, generated, trajectories,
                palette = 'viridis', df_time_key='samples',
                save=True, path=exp_dir, 
                file=f'2d_comparision_local_{epoch}.png',
                x='d1', y='d2', z='d3', is_3d=False
            )

    for epoch in tqdm(range(n_epochs), desc='Epoch'):
        reverse = True if reverse_schema and epoch % reverse_n == 0 else False
        l_loss, b_loss, g_loss = train(
            model, df, groups, optimizer, n_batches, 
            criterion = criterion, use_cuda = use_cuda,
            local_loss=False, global_loss=True, apply_losses_in_time=True,
            hold_one_out=hold_one_out, hold_out=hold_out, 
            hinge_value=hinge_value,
            use_density_loss = use_density_loss,       
            top_k = top_k, lambda_density = lambda_density, density_detach_m=density_detach_m,
            autoencoder = autoencoder, use_emb = use_emb, use_gae = use_gae, sample_size=sample_size, 
            sample_with_replacement=sample_with_replacement, logger=logger, 
            add_noise=add_noise, noise_scale=noise_scale, use_gaussian=use_gaussian,
            use_penalty=use_penalty, lambda_energy=lambda_energy, reverse=reverse,
            lambda_m=lambda_m, lambda_m2=lambda_m2,
            use_penalty_m=use_penalty_m, lambda_energy_m=lambda_energy_m,
            lambda_ot=lambda_ot,
            energy_weighted=energy_weighted,
            energy_detach_m=energy_detach_m,
            threshold_factor=threshold_factor,
            detach_x=detach_x,
            detach_m=detach_m,
            diffusion_lambda_energy=diffusion_lambda_energy,
            diffusion_lambda_energy_m=diffusion_lambda_energy_m, 
            diffusion_energy_weighted=diffusion_energy_weighted,
            diffusion_energy_detach_m=diffusion_energy_detach_m,
        )
        for k, v in l_loss.items():  
            local_losses[k].extend(v)
        batch_losses.extend(b_loss)
        globe_losses.extend(g_loss)
        if plot_every is not None and epoch % plot_every == 0:
            generated, trajectories = generate_plot_data(
                model, df, n_points, n_trajectories, n_bins, 
                sample_with_replacement=sample_with_replacement, use_cuda=use_cuda, 
                samples_key='samples', logger=logger,
                autoencoder=autoencoder, recon=recon
            )
            plot_comparision(
                df, generated, trajectories,
                palette = 'viridis', df_time_key='samples',
                save=True, path=exp_dir, 
                file=f'2d_comparision_local_{n_local_epochs}_global_{epoch}.png',
                x='d1', y='d2', z='d3', is_3d=False
            )
        
    for epoch in tqdm(range(n_post_local_epochs), desc='Posttraining Epoch'):
        reverse = True if reverse_schema and epoch % reverse_n == 0 else False

        l_loss, b_loss, g_loss = train(
            model, df, groups, optimizer, n_batches, 
            criterion = criterion, use_cuda = use_cuda,
            local_loss=True, global_loss=False, apply_losses_in_time=True,
            hold_one_out=hold_one_out, hold_out=hold_out, 
            hinge_value=hinge_value,
            use_density_loss = use_density_loss,       
            top_k = top_k, lambda_density = lambda_density, density_detach_m=density_detach_m,
            autoencoder = autoencoder, use_emb = use_emb, use_gae = use_gae, sample_size=sample_size, 
            sample_with_replacement=sample_with_replacement, logger=logger, 
            add_noise=add_noise, noise_scale=noise_scale, use_gaussian=use_gaussian,
            use_penalty=use_penalty, lambda_energy=lambda_energy, reverse=reverse,
            lambda_m=lambda_m, lambda_m2=lambda_m2,
            use_penalty_m=use_penalty_m, lambda_energy_m=lambda_energy_m,
            lambda_ot=lambda_ot,
            energy_weighted=energy_weighted,
            energy_detach_m=energy_detach_m,
            threshold_factor=threshold_factor,
            detach_x=detach_x,
            detach_m=detach_m,
            diffusion_lambda_energy=diffusion_lambda_energy,
            diffusion_lambda_energy_m=diffusion_lambda_energy_m, 
            diffusion_energy_weighted=diffusion_energy_weighted,
            diffusion_energy_detach_m=diffusion_energy_detach_m,
        )
        for k, v in l_loss.items():  
            local_losses[k].extend(v)
        batch_losses.extend(b_loss)
        globe_losses.extend(g_loss)
        if plot_every is not None and epoch % plot_every == 0:
            generated, trajectories = generate_plot_data(
                model, df, n_points, n_trajectories, n_bins, 
                sample_with_replacement=sample_with_replacement, use_cuda=use_cuda, 
                samples_key='samples', logger=logger,
                autoencoder=autoencoder, recon=recon
            )
            plot_comparision(
                df, generated, trajectories,
                palette = 'viridis', df_time_key='samples',
                save=True, path=exp_dir, 
                file=f'2d_comparision_local_{n_local_epochs}_global_{n_epochs}_post_{epoch}.png',
                x='d1', y='d2', z='d3', is_3d=False
            )

    if reverse_schema:
        _temp = {}
        if hold_one_out:
            for (t0, t1) in generate_steps([g for g in groups if g != hold_out]):
                a = f'{t0}:{t1}'
                b = f'{t1}:{t0}'
                _temp[a] = []
                for i, value in enumerate(local_losses[a]):

                    if i % reverse_n == 0:
                        _temp[a].append(local_losses[b].pop(0))
                        _temp[a].append(value)
                    else:
                        _temp[a].append(value)
            local_losses = _temp
        else:
            for (t0, t1) in generate_steps(groups):
                a = f'{t0}:{t1}'
                b = f'{t1}:{t0}'
                _temp[a] = []
                for i, value in enumerate(local_losses[a]):

                    if i % reverse_n == 0:
                        _temp[a].append(local_losses[b].pop(0))
                        _temp[a].append(value)
                    else:
                        _temp[a].append(value)
            local_losses = _temp



    if plot_every is not None:
        plot_losses(
            local_losses, batch_losses, globe_losses, 
            save=True, path=exp_dir, 
            file=f'losses_l{n_local_epochs}_e{n_epochs}_ple{n_post_local_epochs}.png'
        )

    return local_losses, batch_losses, globe_losses
