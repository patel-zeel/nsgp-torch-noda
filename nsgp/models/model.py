import torch
import numpy as np
from ..utils.inducing_functions import f_kmeans
torch.pi = torch.tensor(np.pi)


class NSGP(torch.nn.Module):
    def __init__(self, X, y, num_inducing_points=5, f_inducing=f_kmeans,
                 jitter=10**-8, random_state=None, local_noise=True, local_std=True):
        super().__init__()

        assert len(
            X.shape) == 2, "X is expected to have shape (n, m) but has shape "+str(X.shape)
        assert len(
            y.shape) == 2, "y is expected to have shape (n, 1) but has shape "+str(y.shape)
        assert y.shape[1] == 1, "y is expected to have shape (n, 1) but has shape "+str(
            y.shape)

        self.X = X
        self.y = y

        self.N = self.X.shape[0]
        self.input_dim = self.X.shape[1]

        # Defining X_bar (Locations where latent lengthscales are to be learnt)
        self.X_bar = torch.tensor(f_inducing(
            self.X, num_inducing_points, random_state), dtype=self.X.dtype)

        self.num_inducing_points = num_inducing_points
        self.jitter = jitter
        self.local_noise = local_noise
        self.local_std = local_std

        # Local params
        self.local_gp_ls = self.param((self.input_dim,))
        self.local_gp_std = self.param((self.input_dim,))
        if not self.local_std:
            self.local_gp_std.requires_grad = False
        self.local_gp_noise_std = self.param((self.input_dim,))
        if not self.local_noise:
            self.local_gp_noise_std.requires_grad = False
        self.local_ls = self.param(
            (self.num_inducing_points, self.input_dim))

        # Global params
        self.global_gp_std = self.param((1,))
        self.global_gp_noise_std = self.param((1,))

        # Initialize model parameters
        self.initialize_params()

    def param(self, shape, requires_grad=True):
        return torch.nn.Parameter(torch.empty(shape, dtype=self.X.dtype), requires_grad=requires_grad)

    def initialize_params(self):
        for param in self.parameters():
            torch.nn.init.normal_(param, mean=0.0, std=1.0)

    def LocalKernel(self, x1, x2, dim):  # kernel of local gp (GP_l)
        dist = torch.square(x1 - x2.T)
        scaled_dist = dist/self.local_gp_ls[dim]**2

        return self.local_gp_std[dim]**2 * torch.exp(-0.5*scaled_dist)

    def get_LS(self, X):  # Infer lengthscales for train_X (self.X)
        l_list = []
        if self.training:
            B = []
        for dim in range(self.input_dim):
            k = self.LocalKernel(
                self.X_bar[:, dim, None], self.X_bar[:, dim, None], dim)
            k = k + torch.eye(self.num_inducing_points, dtype=self.X.dtype) * \
                self.local_gp_noise_std[dim]**2
            c = torch.linalg.cholesky(k)
            alpha = torch.cholesky_solve(
                torch.log(torch.abs(self.local_ls[:, dim, None])), c)
            k_star = self.LocalKernel(
                X[:, dim, None], self.X_bar[:, dim, None], dim)
            l = torch.exp(k_star@alpha)
            l_list.append(l)

            if self.training:
                k_star = self.LocalKernel(
                    X[:, dim, None], self.X_bar[:, dim, None], dim)
                k_star_star = self.LocalKernel(
                    X[:, dim, None], X[:, dim, None], dim)

                chol = torch.linalg.cholesky(k)
                v = torch.cholesky_solve(k_star.T, chol)
                k_post = k_star_star - k_star@v
                k_post = k_post + \
                    torch.eye(self.N)*self.jitter
                post_chol = torch.linalg.cholesky(k_post)
                B.append(torch.log(post_chol.diagonal()))

        if self.training:
            return l_list, B
        else:
            return l_list

    def GlobalKernel(self, X1, X2):  # global GP (GP_y)
        if self.training:
            l, B = self.get_LS(X1)
            l = torch.cat(l, dim=1)
            l1 = l[:, None, :]
            l2 = l[None, :, :]
        else:
            l1 = torch.cat(self.get_LS(X1), dim=1)[:, None, :]
            l2 = torch.cat(self.get_LS(X2), dim=1)[None, :, :]

        lsq = torch.square(l1) + torch.square(l2)
        suffix = torch.sqrt(2 * l1 * l2 / lsq).prod(axis=2)
        dist = torch.square(X1[:, None, :] - X2[None, :, :])
        scaled_dist = dist/lsq
        K = self.global_gp_std**2 * \
            suffix * torch.exp(-scaled_dist.sum(dim=2))

        if self.training:
            return K, B
        else:
            return K

    def nlml(self):
        K, B = self.GlobalKernel(self.X, self.X)
        K = K + torch.eye(self.N) * self.global_gp_noise_std**2
        L = torch.linalg.cholesky(K)
        alpha = torch.cholesky_solve(self.y, L)
        A = 0.5*(self.y.T@alpha + torch.sum(torch.log(L.diagonal())) +
                 self.N * torch.log(2*torch.pi))[0, 0]
        B = torch.sum(torch.cat(B)) + 0.5*(self.num_inducing_points *
                                           self.input_dim*torch.log(2*torch.pi))
        return A+B

    def predict(self, X_new):  # Predict at new locations
        K = self.GlobalKernel(self.X, self.X)
        K_star = self.GlobalKernel(X_new, self.X)
        K_star_star = self.GlobalKernel(X_new, X_new)

        L = torch.linalg.cholesky(
            K + torch.eye(self.N) * self.global_gp_noise_std**2)
        alpha = torch.cholesky_solve(self.y, L)

        pred_mean = K_star@alpha

        v = torch.cholesky_solve(K_star.T, L)
        pred_var = K_star_star - K_star@v
        pred_var = pred_var + \
            torch.eye(X_new.shape[0])*self.global_gp_noise_std**2

        return pred_mean, pred_var