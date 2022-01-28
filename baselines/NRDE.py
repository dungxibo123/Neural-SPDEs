# adapted from https://github.com/patrick-kidger/NeuralCDE

import torch
import csv
import itertools
import numpy as np
import torchcde
from .utils import UnitGaussianNormalizer
from utilities import LpLoss, count_params, EarlyStopping

######################
# A CDE model looks like
#
# z_t = z_0 + \int_0^t f_\theta(z_s) dX_s
#
# Where X is your data and f_\theta is a neural network. So the first thing we need to do is define such an f_\theta.
# That's what this CDEFunc class does.
# Here we've built a small single-hidden-layer neural network, whose hidden layer is of width 128.
######################
class CDEFunc(torch.nn.Module):
    def __init__(self, input_channels, hidden_channels):
        ######################
        # input_channels is the number of input channels in the data X. (Determined by the data.)
        # hidden_channels is the number of channels for z_t. (Determined by you!)
        ######################
        super(CDEFunc, self).__init__()
        self.input_channels = input_channels
        self.hidden_channels = hidden_channels

        self.linear1 = torch.nn.Linear(hidden_channels, 128)
        self.linear2 = torch.nn.Linear(128, input_channels * hidden_channels)

    ######################
    # For most purposes the t argument can probably be ignored; unless you want your CDE to behave differently at
    # different times, which would be unusual. But it's there if you need it!
    ######################
    def forward(self, t, z):
        # z has shape (batch, hidden_channels)
        z = self.linear1(z)
        z = z.relu()
        z = self.linear2(z)
        ######################
        # Easy-to-forget gotcha: Best results tend to be obtained by adding a final tanh nonlinearity.
        ######################
        z = z.tanh()
        ######################
        # Ignoring the batch dimension, the shape of the output tensor must be a matrix,
        # because we need it to represent a linear map from R^input_channels to R^hidden_channels.
        ######################
        z = z.view(z.size(0), self.hidden_channels, self.input_channels)
        return z


######################
# Next, we need to package CDEFunc up into a model that computes the integral.
######################
class NeuralRDE(torch.nn.Module):
    def __init__(self, control_channels, input_channels, hidden_channels, output_channels, interval, interpolation="linear"):
        super(NeuralRDE, self).__init__()

        self.func = CDEFunc(control_channels, hidden_channels)
        self.initial = torch.nn.Linear(input_channels, hidden_channels)
        self.readout = torch.nn.Linear(hidden_channels, output_channels)
        self.interpolation = interpolation
        self.interval = interval

    def forward(self, u0, coeffs):
        if self.interpolation == 'cubic':
            X = torchcde.CubicSpline(coeffs)
        elif self.interpolation == 'linear':
            X = torchcde.LinearInterpolation(coeffs)
        else:
            raise ValueError("Only 'linear' and 'cubic' interpolation methods are implemented.")

        ######################
        # Easy to forget gotcha: Initial hidden state should be a function of the first observation.
        ######################
        # X0 = X.evaluate(X.interval[0])
        z0 = self.initial(u0)

        ######################
        # Actually solve the CDE.
        ######################
        z_T = torchcde.cdeint(X=X,
                              z0=z0,
                              func=self.func,
                              # t = X.interval,
                              method='euler',
                              adjoint = False, 
                              t=self.interval)

        ######################
        # Both the initial value and the terminal value are returned from cdeint; extract just the terminal value,
        # and then apply a linear map.
        ######################
        # z_T = z_T[:, 1]
        pred_y = self.readout(z_T)
        return pred_y


#===========================================================================
# Data Loaders
#===========================================================================

def dataloader_nrde_1d(u, xi, ntrain=1000, ntest=200, T=51, sub_t=1, batch_size=20, dim_x=128, depth=2, window_length=10, normalizer=True, interpolation='linear', dataset=None):

    if dataset=='phi41':
        T, sub_t = 51, 1
    elif dataset=='wave':
        T, sub_t = (u.shape[-1]+1)//2, 5

    u_train = u[:ntrain, :dim_x, 0:T:sub_t].permute(0, 2, 1)
    xi_train = xi[:ntrain, :dim_x, 0:T:sub_t].permute(0, 2, 1)
    
    t = torch.linspace(0., xi_train.shape[1], xi_train.shape[1])[None, :, None].repeat(ntrain, 1, 1)
    xi_train = torch.cat([t, xi_train], dim=2)

    u_test = u[-ntest:, :dim_x, 0:T:sub_t].permute(0, 2, 1)
    xi_test = xi[-ntest:, :dim_x, 0:T:sub_t].permute(0, 2, 1)

    t = torch.linspace(0., xi_test.shape[1], xi_test.shape[1])[None, :, None].repeat(ntest, 1, 1)
    xi_test = torch.cat([t,xi_test], dim=2)

    #### get interval where we want the solution #########################################
    xi_train_dummy = torchcde.linear_interpolation_coeffs(xi_train)
    xi_train_dummy = torchcde.LinearInterpolation(xi_train_dummy)
    interval = xi_train_dummy._t
    ######################################################################################

    #### this is what differs from NCDE ##################################################
    xi_train = torchcde.logsig_windows(xi_train, depth, window_length=window_length)
    xi_test = torchcde.logsig_windows(xi_test, depth, window_length=window_length)
    ######################################################################################

    if normalizer:
        xi_normalizer = UnitGaussianNormalizer(xi_train)
        xi_train = xi_normalizer.encode(xi_train)
        xi_test = xi_normalizer.encode(xi_test)

        normalizer = UnitGaussianNormalizer(u_train)
        u_train = normalizer.encode(u_train)
        u_test = normalizer.encode(u_test)

    u0_train = u_train[:, 0, :]
    u0_test = u_test[:, 0, :]

    # interpolation
    if interpolation=='linear':
        xi_train = torchcde.linear_interpolation_coeffs(xi_train)
        xi_test = torchcde.linear_interpolation_coeffs(xi_test)
    elif interpolation=='cubic':
        xi_train = torchcde.hermite_cubic_coefficients_with_backward_differences(xi_train)
        xi_test = torchcde.hermite_cubic_coefficients_with_backward_differences(xi_test)

    train_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(u0_train, xi_train, u_train), batch_size=batch_size, shuffle=True)
    test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(u0_test, xi_test, u_test), batch_size=batch_size, shuffle=False)

    return train_loader, test_loader, interval, xi_train.size(-1), normalizer

#===========================================================================
# Training and Testing functionalities (same as NCDE)
#===========================================================================

def eval_nrde_1d(model, test_dl, myloss, batch_size, device, u_normalizer=None):

    ntest = len(test_dl.dataset)
    test_loss = 0.
    with torch.no_grad():
        for u0_, xi_, u_ in test_dl:    
            loss = 0.       
            u0_, xi_, u_ = u0_.to(device), xi_.to(device), u_.to(device)
            u_pred = model(u0_, xi_)

            if u_normalizer is not None:
                u_pred = u_normalizer.decode(u_pred.cpu())
                u_ = u_normalizer.decode(u_.cpu())

            loss = myloss(u_pred[:, 1:, :].reshape(batch_size, -1), u_[:, 1:, :].reshape(batch_size, -1))
            test_loss += loss.item()
    print('Test Loss: {:.6f}'.format(test_loss / ntest))
    return test_loss / ntest

def train_nrde_1d(model, train_loader, test_loader, u_normalizer, device, myloss, batch_size=20, epochs=5000, learning_rate=0.001, scheduler_step=100, scheduler_gamma=0.5, print_every=20, plateau_patience=None, plateau_terminate=None, checkpoint_file='checkpoint.pt'):

    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-4)

    if plateau_patience is None:
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=scheduler_step, gamma=scheduler_gamma)
    else:
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=plateau_patience, threshold=1e-6, min_lr=1e-7)
    if plateau_terminate is not None:
        early_stopping = EarlyStopping(patience=plateau_terminate, verbose=False, path=checkpoint_file)

    ntrain = len(train_loader.dataset)
    ntest = len(test_loader.dataset)

    losses_train = []
    losses_test = []

    try:

        for ep in range(epochs):

            model.train()
            
            train_loss = 0.
            for u0_, xi_, u_ in train_loader:

                loss = 0.
                
                u0_ = u0_.to(device)
                xi_ = xi_.to(device)
                u_ = u_.to(device)

                u_pred = model(u0_, xi_)
                
                if u_normalizer is not None:
                    u_pred = u_normalizer.decode(u_pred.cpu())
                    u_ = u_normalizer.decode(u_.cpu())
                
                loss = myloss(u_pred[:, 1:, :].reshape(batch_size, -1), u_[:, 1:, :].reshape(batch_size, -1))

                train_loss += loss.item()
                loss.backward()
                optimizer.step()
                optimizer.zero_grad()

            test_loss = 0.
            with torch.no_grad():
                for u0_, xi_, u_ in test_loader:
                    
                    loss = 0.
                    
                    u0_ = u0_.to(device)
                    xi_ = xi_.to(device)
                    u_ = u_.to(device)

                    u_pred = model(u0_, xi_)

                    if u_normalizer is not None:
                        u_pred = u_normalizer.decode(u_pred.cpu())
                        u_ = u_normalizer.decode(u_.cpu())

                    loss = myloss(u_pred[:, 1:, :].reshape(batch_size, -1), u_[:, 1:, :].reshape(batch_size, -1))

                    test_loss += loss.item()
            
            if plateau_patience is None:
                scheduler.step()
            else:
                scheduler.step(test_loss/ntest)
            if plateau_terminate is not None:
                early_stopping(test_loss/ntest, model)
                if early_stopping.early_stop:
                    print("Early stopping")
                    break

            if ep % print_every == 0:
                losses_train.append(train_loss/ntrain)
                losses_test.append(test_loss/ntest)
                print('Epoch {:04d} | Total Train Loss {:.6f} | Total Test Loss {:.6f}'.format(ep, train_loss / ntrain, test_loss / ntest))

        return model, losses_train, losses_test

    except KeyboardInterrupt:

        return model, losses_train, losses_test


def hyperparameter_search_nrde(train_dl, val_dl, test_dl, noise_size, I, dim_x, u_normalizer=None, d_h=[32], epochs=500, print_every=20, lr=0.025, plateau_patience=100, plateau_terminate=100, log_file ='log_nspde', checkpoint_file='checkpoint.pt', final_checkpoint_file='final.pt'):

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    hyperparams = d_h #list(itertools.product(d_h))

    loss = LpLoss(size_average=False)
    
    fieldnames = ['d_h', 'nb_params', 'loss_train', 'loss_val', 'loss_test']
    with open(log_file, 'w', encoding='UTF8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        
    best_loss_val = 1000.

    for _dh in hyperparams:
        
        print('\n dh:{}'.format(_dh))
        
        model = NeuralRDE(control_channels=noise_size, input_channels=dim_x, 
                  hidden_channels=_dh, output_channels=dim_x, interval=I, 
                  interpolation='linear').cuda()

        nb_params = count_params(model)
        
        print('\n The model has {} parameters'. format(nb_params))

        # Train the model. The best model is checkpointed.
        _, _, _ = train_nrde_1d(model, train_dl, val_dl, u_normalizer, device, loss, batch_size=20, epochs=epochs, learning_rate=lr, scheduler_step=500, scheduler_gamma=0.5, print_every=print_every, plateau_patience=plateau_patience, plateau_terminate=plateau_terminate, checkpoint_file=checkpoint_file)

        # load the best trained model 
        model.load_state_dict(torch.load(checkpoint_file))
        
        # compute the test loss 
        loss_test = eval_nrde_1d(model, test_dl, loss, 20, device, u_normalizer=u_normalizer)

        # we also recompute the validation and train loss
        loss_train = eval_nrde_1d(model, train_dl, loss, 20, device, u_normalizer=u_normalizer)
        loss_val = eval_nrde_1d(model, val_dl, loss, 20, device, u_normalizer=u_normalizer)

        # if this configuration of hyperparameters is the best so far (determined wihtout using the test set), save it 
        if loss_val < best_loss_val:
            torch.save(model.state_dict(), final_checkpoint_file)
            best_loss_val = loss_val

        # write results
        with open(log_file, 'a', encoding='UTF8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([_dh, nb_params, loss_train, loss_val, loss_test])