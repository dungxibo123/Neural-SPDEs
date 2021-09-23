#TODO: make it compatible with torch version
# adapted from https://github.com/zongyi-li/fourier_neural_operator. 
# we replace the deterministic forcing with a random noise which is a Wiener process 
# in two dimensions following Example 10.3 and 10.12 in the book
# An Introduction to Computational Stochastic PDEs

import torch

import math

import matplotlib.pyplot as plt
import matplotlib

from random_forcing import WienerInc, GaussianRF

from timeit import default_timer

import scipy.io

#w0: initial vorticity
#f_h: forcing term already in Fourier space
#visc: viscosity (1/Re)
#T: final time
#delta_t: internal time-step for solve (descrease if blow-up)
#record_steps: number of in-time snapshots to record
def navier_stokes_2d(w0, f, visc, T, delta_t=1e-4, record_steps=1, dW_sampler=None):

    #Grid size - must be power of 2
    N = w0.size()[-1]

    #Maximum frequency
    k_max = math.floor(N/2.0)

    #Number of steps to final time
    steps = math.ceil(T/delta_t)

    #Initial vorticity to Fourier space
    w_h = torch.fft.fftn(w0, dim=[1,2])
    w_h = torch.stack([w_h.real, w_h.imag],dim=-1)

    #Forcing to Fourier space
    f_h = torch.fft.fftn(f, dim=[-2,-1])
    f_h = torch.stack([f_h.real, f_h.imag],dim=-1) 
    #If same forcing for the whole batch 
    if len(f_h.size()) < len(w_h.size()):
        f_h = torch.unsqueeze(f_h, 0)
    
    #If stochastic forcing
    if dW_sampler is not None:
        dW_h = dW_sampler.sampledW(w0.shape[0], delta_t, iFspace=True)
        f_sto_h = math.sqrt(visc)*dW_h/delta_t

    #Record solution every this number of steps
    record_time = math.floor(steps/record_steps)

    #Wavenumbers in y-direction
    k_y = torch.cat((torch.arange(start=0, end=k_max, step=1, device=w0.device), torch.arange(start=-k_max, end=0, step=1, device=w0.device)), 0).repeat(N,1)
    #Wavenumbers in x-direction
    k_x = k_y.transpose(0,1)
    #Negative Laplacian in Fourier space
    lap = 4*(math.pi**2)*(k_x**2 + k_y**2)
    lap[0,0] = 1.0
    #Dealiasing mask
    dealias = torch.unsqueeze(torch.logical_and(torch.abs(k_y) <= (2.0/3.0)*k_max, torch.abs(k_x) <= (2.0/3.0)*k_max).float(), 0)

    #Saving solution and time
    sol = torch.zeros(*w0.size(), record_steps, device=w0.device)
    sol_t = torch.zeros(record_steps, device=w0.device)

    #Record counter
    c = 0
    #Physical time
    t = 0.0
    for j in range(steps):
        #Stream function in Fourier space: solve Poisson equation
        psi_h = w_h.clone()
        psi_h[...,0] = psi_h[...,0]/lap
        psi_h[...,1] = psi_h[...,1]/lap

        #Velocity field in x-direction = psi_y
        q = psi_h.clone()
        temp = q[...,0].clone()
        q[...,0] = -2*math.pi*k_y*q[...,1]
        q[...,1] = 2*math.pi*k_y*temp
        q = torch.fft.ifftn(torch.view_as_complex(q), dim=[1,2], s=(N,N)).real

        #Velocity field in y-direction = -psi_x
        v = psi_h.clone()
        temp = v[...,0].clone()
        v[...,0] = 2*math.pi*k_x*v[...,1]
        v[...,1] = -2*math.pi*k_x*temp
        v = torch.fft.ifftn(torch.view_as_complex(v), dim=[1,2], s=(N,N)).real

        #Partial x of vorticity
        w_x = w_h.clone()
        temp = w_x[...,0].clone()
        w_x[...,0] = -2*math.pi*k_x*w_x[...,1]
        w_x[...,1] = 2*math.pi*k_x*temp
        w_x = torch.fft.ifftn(torch.view_as_complex(w_x), dim=[1,2], s=(N,N)).real

        #Partial y of vorticity
        w_y = w_h.clone()
        temp = w_y[...,0].clone()
        w_y[...,0] = -2*math.pi*k_y*w_y[...,1]
        w_y[...,1] = 2*math.pi*k_y*temp
        w_y = torch.fft.ifftn(torch.view_as_complex(w_y), dim=[1,2], s=(N,N)).real

        #Non-linear term (u.grad(w)): compute in physical space then back to Fourier space
        F_h = torch.fft.fftn(q*w_x + v*w_y, dim=[1,2])
        F_h = torch.stack([F_h.real, F_h.imag],dim=-1) 

        #Dealias
        F_h[...,0] = dealias* F_h[...,0]
        F_h[...,1] = dealias* F_h[...,1]

        #Cranck-Nicholson update
        if dW_sampler:
            w_h[...,0] = (-delta_t*F_h[...,0] + delta_t*f_h[...,0] + delta_t*f_sto_h[...,0] + (1.0 - 0.5*delta_t*visc*lap)*w_h[...,0])/(1.0 + 0.5*delta_t*visc*lap)
            w_h[...,1] = (-delta_t*F_h[...,1] + delta_t*f_h[...,1] + delta_t*f_sto_h[...,1] + (1.0 - 0.5*delta_t*visc*lap)*w_h[...,1])/(1.0 + 0.5*delta_t*visc*lap)
        else:
            w_h[...,0] = (-delta_t*F_h[...,0] + delta_t*f_h[...,0] + (1.0 - 0.5*delta_t*visc*lap)*w_h[...,0])/(1.0 + 0.5*delta_t*visc*lap)
            w_h[...,1] = (-delta_t*F_h[...,1] + delta_t*f_h[...,1] + (1.0 - 0.5*delta_t*visc*lap)*w_h[...,1])/(1.0 + 0.5*delta_t*visc*lap)

        #Update real time (used only for recording)
        t += delta_t

        if (j+1) % record_time == 0:
            #Solution in physical space
            w = torch.fft.ifftn(torch.view_as_complex(w_h), dim=[1,2], s=(N,N)).real

            #Record solution and time
            sol[...,c] = w
            sol_t[c] = t

            c += 1


    return sol, sol_t


# device = torch.device('cuda')

# nu = 1e-3

# # Spatial Resolution
# s = 256
# sub = 1

# # Temporal Resolution   
# T = 50.0
# delta_t = 1e-4
# steps = math.ceil(T/delta_t)

# #Number of solutions to generate
# N = 20

# #Set up 2d GRF with covariance parameters
# GRF = GaussianRF(2, s, alpha=2.5, tau=7, device=device)

# #Forcing function: 0.1*(sin(2pi(x+y)) + cos(2pi(x+y)))
# # t = torch.linspace(0, 1, s+1, device=device)
# # t = t[0:-1]

# # X,Y = torch.meshgrid(t, t)
# # f = 0.1*(torch.sin(2*math.pi*(X + Y)) + torch.cos(2*math.pi*(X + Y)))

# #Forcing function: sqrt(nu)dW/dt in Fourier space
# dW_Sampler = WienerInc(a=1,J=s,alpha=0.05, device=device) 


# #Number of snapshots from solution
# record_steps = 200

# #Inputs
# a = torch.zeros(N, s, s)
# #Solutions
# u = torch.zeros(N, s, s, record_steps)

# #Solve equations in batches (order of magnitude speed-up)

# #Batch size
# bsize = 20

# c = 0
# t0 =default_timer()
# for j in range(N//bsize):

#     #Sample random feilds
#     w0 = GRF.sample(bsize)

#     #Sample random forcing (sqrt(nu)dW/dt in Fourier space) but does not fit in memory
#     # dW_h = dW_Sampler.sampleseriessdW(bsize, steps, delta_t, iFspace=True)
#     # forcing_h = sqrt(nu)*dW_h/delta_t

#     #Solve NS
#     sol, sol_t = navier_stokes_2d(w0, dW_Sampler, nu, T, delta_t, record_steps)  # w0, f_h, visc, T, delta_t, record_steps

#     a[c:(c+bsize),...] = w0
#     u[c:(c+bsize),...] = sol

#     c += bsize
#     t1 = default_timer()
#     print(j, c, t1-t0)

# scipy.io.savemat('ns_data.mat', mdict={'a': a.cpu().numpy(), 'u': u.cpu().numpy(), 't': sol_t.cpu().numpy()})