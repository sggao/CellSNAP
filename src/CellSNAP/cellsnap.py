import torch
from torch.utils.data import Dataset
from torchvision import transforms
import os
import numpy as np
import pandas as pd
import utils
import graph
import preprocessing
import anndata as ad
import scanpy as sc
from models import *
import torch.optim as optim
from matplotlib import pyplot as plt


class CellSNAP:

    def __init__(self,
                 dataset,
                 device,
                 cnn_model=None,
                 cnn_latent_dim=128,
                 gnn_latent_dim=33,
                 proj_dim=32,
                 fc_out_dim=33,
                 cnn_out_dim=11):
        self.dataset = dataset
        self.device = device
        self.output_dim = self.dataset.cell_nbhd.shape[1]
        self.n_cell = self.dataset.cell_nbhd.shape[0]
        self.gnn_latent_dim = gnn_latent_dim
        self.cnn_latent_dim = cnn_latent_dim
        self.embed_dim = fc_out_dim + cnn_out_dim
        self.cnn_model = cnn_model
        if self.cnn_model:
            self.cnn_model = SNAP_CNN(cnn_latent_dim, self.output_dim)
            self.gnn_model = SNAP_GNN_DUO(
                out_dim=self.output_dim,
                feature_input_dim=dataset.features.shape[1],
                cnn_input_dim=cnn_latent_dim,
                gnn_latent_dim=gnn_latent_dim,
                proj_dim=proj_dim,
                fc_out_dim=fc_out_dim,
                cnn_out_dim=cnn_out_dim)
        else:
            self.gnn_model = SNAP_GNN_LITE(out_dim=self.output_dim,
                                           input_dim=dataset.features.shape[1],
                                           gnn_latent_dim=self.gnn_latent_dim)
        return

    def fit_snap_cnn(self,
                     batch_size=64,
                     learning_rate=1e-4,
                     n_epochs=300,
                     loss_fn='MSELoss',
                     OptimizerAlg='Adam',
                     optimizer_kwargs=None,
                     SchedulerAlg=None,
                     scheduler_kwargs=None,
                     print_every=10):
        print(
            '\n=============Training convolutional neural network============\n',
            flush=True)
        # enable data augmentation
        self.dataset.use_transform = True
        dataloader = torch.utils.data.DataLoader(self.dataset,
                                                 batch_size=batch_size,
                                                 shuffle=True,
                                                 num_workers=1)
        criterion = getattr(nn, loss_fn, nn.MSELoss)()

        self.cnn_model = self.cnn_model.to(self.device)
        optimizer, scheduler = utils.get_optimizer_and_scheduler(
            self.cnn_model.parameters(), OptimizerAlg, {
                'lr': learning_rate,
                **optimizer_kwargs
            }, SchedulerAlg, scheduler_kwargs)
        criterion.to(self.device)
        self.cnn_model.train()
        for epoch in range(1, 1 +
                           n_epochs):  # loop over the dataset multiple times

            running_loss = 0.0
            running_sample = 0
            for i, data in enumerate(dataloader, 0):
                # get the inputs; data is a list of [inputs, labels]
                inputs, labels = data
                inputs = inputs.to(self.device).to(torch.float32)
                labels = labels.to(self.device).to(torch.float32)

                # zero the parameter gradients
                optimizer.zero_grad()

                # forward + backward + optimize
                outputs = self.cnn_model(inputs)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
                # change learning rate
                if scheduler:
                    scheduler.step()

                running_loss += loss.item() * inputs.shape[0]
                running_sample += inputs.shape[0]
                if i % 100 == 99:
                    print(
                        f'===Epoch {epoch}, Step {i + 1:5d} loss: {running_loss / running_sample:.6f}==='
                    )

            if epoch % print_every == 0:
                print(
                    f'===Epoch {epoch}, Step {i + 1:5d} loss: {running_loss / running_sample:.6f}===',
                    flush=True)

        return

    def get_cnn_embedding(self, batch_size=512, path2result=None):
        self.dataset.use_transform = False
        testloader = torch.utils.data.DataLoader(self.dataset,
                                                 batch_size=batch_size,
                                                 shuffle=False,
                                                 num_workers=1)
        self.cnn_model.to(self.device)
        self.cnn_model.eval()
        self.cnn_embedding = np.zeros(
            (self.dataset.df.shape[0], self.cnn_latent_dim))
        start_idx = 0
        with torch.no_grad():
            for data in testloader:
                inputs, labels = data
                inputs = inputs.to(self.device).to(torch.float32)
                outputs = self.cnn_model.cnn_encoder(inputs)
                self.cnn_embedding[start_idx:start_idx +
                                   inputs.shape[0]] = outputs.cpu()

                start_idx += inputs.shape[0]
        assert (start_idx == self.dataset.df.shape[0])
        print('\n=====Save CNN Embedding!============\n', flush=True)
        if path2result:
            if not os.path.exists(path2result):
                os.makedirs(path2result)
            np.save(os.path.join(path2result, "SNAP_CNN_embedding.npy"),
                    self.cnn_embedding)

    def fit_snap_gnn(self,
                     learning_rate=1e-4,
                     n_epochs=3000,
                     loss_fn='MSELoss',
                     OptimizerAlg='Adam',
                     optimizer_kwargs=None,
                     SchedulerAlg=None,
                     scheduler_kwargs=None,
                     print_every=500,
                     verbose=True):
        features = torch.from_numpy(self.dataset.features).float().to(
            self.device)
        features_edges = self.dataset.feature_edges
        edge_index = torch.from_numpy(np.array(features_edges[:2])).long().to(
            self.device)
        cell_nbhd = torch.from_numpy(self.dataset.dual_labels).float().to(
            self.device)
        criterion = getattr(nn, loss_fn, nn.MSELoss)()
        criterion.to(self.device)
        self.gnn_model.to(self.device)
        optimizer, scheduler = utils.get_optimizer_and_scheduler(
            self.gnn_model.parameters(), OptimizerAlg, {
                'lr': learning_rate,
                **optimizer_kwargs
            }, SchedulerAlg, scheduler_kwargs)
        self.gnn_model.train()
        if self.cnn_model:
            cnn_embedding = torch.from_numpy(self.cnn_embedding).float().to(
                self.device)
        for e in range(1, 1 + n_epochs):
            if self.cnn_model:
                predicted_nbhd = self.gnn_model(x=features,
                                                cnn_embed=cnn_embedding,
                                                edge_index=edge_index)
            else:
                predicted_nbhd = self.gnn_model(x=features,
                                                edge_index=edge_index)
            # Compute prediction error
            loss = criterion(predicted_nbhd, cell_nbhd)
            # Backpropagation
            optimizer.zero_grad()
            loss.backward()

            # take one step
            optimizer.step()
            if scheduler:
                scheduler.step()

            # record the loss
            curr_train_loss = loss.item()
            if verbose and e % print_every == 0:
                print(
                    f'===Epoch {e}, the training loss is {curr_train_loss:>0.8f}==',
                    flush=True)

        print('\n=========Save CellSNAP Embedding!============\n', flush=True)
        self.gnn_model.eval()
        with torch.no_grad():
            if self.cnn_model:
                gnn_embedding = self.gnn_model.gnn_encoder(
                    x=features, cnn_embed=cnn_embedding,
                    edge_index=edge_index).detach().cpu().numpy()
            else:
                gnn_embedding = self.gnn_model.gnn_encoder(
                    x=features, edge_index=edge_index).detach().cpu().numpy()

        return gnn_embedding

    def get_snap_embedding(self,
                           round=5,
                           k=32,
                           learning_rate=1e-4,
                           n_epochs=3000,
                           loss_fn='MSELoss',
                           OptimizerAlg='Adam',
                           optimizer_kwargs=None,
                           SchedulerAlg=None,
                           scheduler_kwargs=None,
                           verbose=True):
        """
        Parameters
        ----------

        round : int
            number of times to fit SNAP-GNN
        k : int
            dimension reduction on final outputs

        """
        dim = self.embed_dim
        concat_embedding = np.zeros((self.n_cell, round * dim))
        for i in range(round):
            gnn_embedding = self.fit_snap_gnn(
                learning_rate=learning_rate,
                n_epochs=n_epochs,
                loss_fn=loss_fn,
                OptimizerAlg=OptimizerAlg,
                optimizer_kwargs=optimizer_kwargs,
                SchedulerAlg=SchedulerAlg,
                scheduler_kwargs=scheduler_kwargs,
                print_every=500,
                verbose=verbose)
            concat_embedding[:, i * dim:(i + 1) * dim] = gnn_embedding
        Ue, Se, Vhe = np.linalg.svd(concat_embedding, full_matrices=False)
        gnn_embedding = Ue[:, :k] @ np.diag(Se[:k])
        self.snap_embedding = gnn_embedding
        return

    def fit_transform(self,
                      cnn_batch_size=64,
                      cnn_learning_rate=1e-4,
                      cnn_epochs=300,
                      cnn_loss_fn='MSELoss',
                      cnn_print=10,
                      round=5,
                      k=32,
                      gnn_learning_rate=1e-3,
                      gnn_epochs=3000,
                      gnn_loss_fn='MSELoss',
                      optim='Adam',
                      optim_kwargs=None,
                      sche=None,
                      sche_kwargs=None,
                      verbose=True,
                      path2result=None):
        if self.cnn_model:
            self.fit_snap_cnn(self,
                              batch_size=cnn_batch_size,
                              learning_rate=cnn_learning_rate,
                              n_epochs=cnn_epochs,
                              loss_fn=cnn_loss_fn,
                              OptimizerAlg=optim,
                              optimizer_kwargs=optim_kwargs,
                              SchedulerAlg=sche,
                              scheduler_kwargs=sche_kwargs,
                              print_every=cnn_print)
            self.get_cnn_embedding(self,
                                   batch_size=512,
                                   path2result=path2result)

        self.get_snap_embedding(self,
                                round=round,
                                k=k,
                                learning_rate=gnn_learning_rate,
                                n_epochs=gnn_epochs,
                                loss_fn=gnn_loss_fn,
                                OptimizerAlg=optim,
                                optimizer_kwargs=optim_kwargs,
                                SchedulerAlg=sche,
                                scheduler_kwargs=sche_kwargs,
                                print_every=cnn_print,
                                verbose=verbose)

        return
    
    def get_snap_clustering(self, neighbor=15, resolution=1.0):
        # compute cell clustering based on SNAP embedding
        feature_labels = self.dataset.feature_labels
        embedding = self.snap_embedding
        snap_adata = ad.AnnData(utils.drop_zero_variability_columns(embedding))
        snap_adata.obs['input'] = feature_labels
        sc.pp.scale(snap_adata)
        sc.pp.neighbors(snap_adata, n_neighbors=neighbor, use_rep='X')
        sc.tl.umap(snap_adata)
        sc.tl.leiden(snap_adata, resolution=resolution)
        # clean cluster
        from utils import cluster_refine, clean_cluster
        snap_refine = cluster_refine(label = snap_adata.obs['leiden'], label_ref=snap_adata.obs['input'])
        snap_clean = clean_cluster(snap_refine)
        self.snap_clustering = snap_clean
        return 

    def visualize_umap(self, embedding, label):
        # visualization of umap of the embedding
        adata = ad.AnnData(embedding)
        sc.pp.scale(adata)
        adata.obs['annotation'] = list(label)
        sc.tl.pca(adata, svd_solver='arpack')
        sc.pp.neighbors(adata, n_neighbors=10)
        sc.tl.umap(adata)
        plt.rcParams["figure.figsize"] = (10, 8)
        sc.pl.umap(adata, color='annotation', legend_fontsize=17, show=False)
        plt.show()
        return
