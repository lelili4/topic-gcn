import tensorflow as tf
import math
import numpy as np
from collections import namedtuple

import src.loss as loss
from src.layer import MeanAggregator, AttentionAggregator, ChannelAggregator, ChannelVAE

# LayerInfo is a namedtuple that specifies the parameters 
# of the recursive layers
LayerInfo = namedtuple("LayerInfo",
    ['layer_name', # name of the layer (to get feature embedding etc.)
     'neighbor_sampler', # callable neigh_sampler constructor
     'num_samples', # num of sampled neighbor
     'output_dim', # the output (i.e., hidden) dimension
     'num_head' # num of head for GAT
])

class UnsupervisedSAGE(object):
    """
    Unsupervised GraphSAGE
    """
    def __init__(self, placeholders, features, adj, degrees, layer_infos, args):
        self.inputs1 = placeholders['batch1']
        self.inputs2 = placeholders['batch2']
        self.batch_size = placeholders['batch_size']
        self.placeholders = placeholders
        
        self.adj_info = adj
        self.features = tf.Variable(tf.constant(features, dtype=tf.float32), trainable=False)
        self.degrees = degrees
        self.neg_sample_size = args.neg_sample
        
        self.dims = [features.shape[1]]
        self.dims.extend([layer_infos[i].output_dim for i in range(len(layer_infos))])
        self.layer_infos = layer_infos
        
        self.optimizer = tf.train.AdamOptimizer(learning_rate=args.learning_rate)
        self.weight_decay = args.weight_decay
        
        self.build()
        
    def build(self):
        self._build()
        self._loss()
        self._accuracy()
        
        self.loss = self.loss / tf.cast(self.batch_size, tf.float32)
        grads_and_vars = self.optimizer.compute_gradients(self.loss)
        clipped_grads_and_vars = [(tf.clip_by_value(grad, -5.0, 5.0) if grad is not None else None, var)
                                 for grad, var in grads_and_vars]
        self.opt_op = self.optimizer.apply_gradients(clipped_grads_and_vars)
        
    def _build(self):
        # negative sampling
        labels = tf.reshape(tf.cast(self.placeholders['batch2'], dtype=tf.int64), [self.batch_size, 1])
        self.neg_samples, _, _ = tf.nn.fixed_unigram_candidate_sampler(
            true_classes=labels,
            num_true=1,
            num_sampled=self.neg_sample_size,
            unique=False,
            range_max=len(self.degrees),
            distortion=0.75,
            unigrams=self.degrees.tolist()
        )
        
        # convolution for three set of nodes
        # sample layers of nodes
        samples1, support_sizes1, _ = self.sample(self.inputs1, self.batch_size)
        samples2, support_sizes2, _ = self.sample(self.inputs2, self.batch_size)
        neg_samples, neg_support_sizes, _ = self.sample(self.neg_samples, self.neg_sample_size)
        
        # initialize the aggregators
        self.init_aggregator()
        
        # aggregate
        self.outputs1 = self.aggregate(samples1, support_sizes1, self.batch_size)
        self.outputs2 = self.aggregate(samples2, support_sizes2, self.batch_size)
        self.neg_outputs = self.aggregate(neg_samples, neg_support_sizes, self.neg_sample_size)
        self.outputs1 = tf.nn.l2_normalize(self.outputs1, 1)
        self.outputs2 = tf.nn.l2_normalize(self.outputs2, 1)
        self.neg_outputs = tf.nn.l2_normalize(self.neg_outputs, 1)
            
    def _loss(self):
        self.loss = loss.xent_loss(self.outputs1, self.outputs2, self.neg_outputs)
        for layer in range(len(self.layer_infos) + 1):
            for var in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='aggregate/layer_' + str(layer)):
                self.loss += self.weight_decay * tf.nn.l2_loss(var)
        
    def _accuracy(self):
        aff = loss.affinity(self.outputs1, self.outputs2)
        neg_aff = loss.neg_cost(self.outputs1, self.neg_outputs)
        neg_aff = tf.reshape(neg_aff, [self.batch_size, self.neg_sample_size])
        _aff = tf.expand_dims(aff, axis=1)
        aff_all = tf.concat(axis=1, values=[neg_aff, _aff])
        size = tf.shape(aff_all)[1]
        _, indices_of_ranks = tf.nn.top_k(aff_all, k=size)
        _, ranks = tf.nn.top_k(-indices_of_ranks, k=size)
        self.mrr = tf.reduce_mean(tf.div(1.0, tf.cast(ranks[:, -1] + 1, tf.float32)))
        
    def sample(self, inputs, batch_size):
        """
        Sample neighbors to be the supportive set for convolution
        """
        inputs = tf.cast(inputs, dtype=tf.int32)
        samples = [inputs]
        edges = []
        support_size = 1
        support_sizes = [support_size]
        for k in range(len(self.layer_infos)):
            # expanding neighbors of input nodes layer by layer backward
            # layer_info: forward, samples: backward
            t = len(self.layer_infos) - k - 1
            support_size *= self.layer_infos[t].num_samples
            sampler = self.layer_infos[t].neighbor_sampler
            node = sampler((samples[k], self.layer_infos[t].num_samples)) # [batch_size, num_samples]
            # concatenate to construct all pairs
            neighbors = tf.expand_dims(node, axis=2) # [batch_size, num_samples, 1]
            curnodes = tf.stack([samples[k] for i in range(self.layer_infos[t].num_samples)]) # [num_samples, batch_size]
            curnodes = tf.transpose(curnodes, [1, 0]) # [batch_size, num_samples]
            curnodes = tf.expand_dims(curnodes, axis=2) # [batch_size, num_samples, 1]
            edge = tf.cast(tf.concat((curnodes, neighbors), axis=2), dtype=tf.int64) # [batch_size, num_samples, 2]
            # flattten
            samples.append(tf.reshape(node, [support_size * batch_size,])) # [batch_size * num_samples, ]
            support_sizes.append(support_size)
            edges.append(tf.reshape(edge, [support_size * batch_size, 2]))
        return samples, support_sizes, edges

    def init_aggregator(self):
        """ Initialize aggregator layers with creating reuseble convolution variables
        """
        self.aggregators = []
        for layer in range(len(self.dims) - 1):
            name = 'layer_' + str(layer)
            dim_mult = 2 if layer != 0 else 1
            if layer == len(self.dims) - 2:
                aggregator = MeanAggregator(name, dim_mult*self.dims[layer], self.dims[layer+1], 
                                            dropout=self.placeholders['dropout'], act=lambda x:x)
            else:
                aggregator = MeanAggregator(name, dim_mult*self.dims[layer], self.dims[layer+1],
                                            dropout=self.placeholders['dropout'])
            self.aggregators.append(aggregator)
    
    def aggregate(self, samples, support_sizes, batch_size):
        """ Aggregate embeddings of neighbors to compute the embeddings at next layer
        Args:
            samples: a list of node samples hops away at each layer. size=K+1
            support_sizes: a list of node numbers at each layer. size=K+1
            batch_size: input size
        Returns:
            The final embedding for input nodes
        """
        hidden = [tf.nn.embedding_lookup([self.features], node_sample) for node_sample in samples]
        num_samples = [layer_info.num_samples for layer_info in self.layer_infos] # neighbor sample size for each node
        for layer in range(len(num_samples)):
            # embedding at current layer for all support nodes hops away
            next_hidden = []
            for hop in range(len(num_samples) - layer):
                dim_mult = 2 if layer != 0 else 1
                neighbor_dims = [batch_size * support_sizes[hop], 
                                 num_samples[len(num_samples) - hop - 1],
                                 dim_mult * self.dims[layer]]
                inputs = (hidden[hop], tf.reshape(hidden[hop + 1], neighbor_dims))
                h = self.aggregators[layer](inputs)
                next_hidden.append(h)
            hidden = next_hidden
        return hidden[0]
                
class UnsupervisedGAT(UnsupervisedSAGE):
    """
    Unsupervised GAT
    """
    def __init__(self, placeholders, features, adj, degrees, layer_infos, args):
        self.heads = [layer_infos[i].num_head for i in range(len(layer_infos))]
        # define heads first, otherwise cannot _build
        UnsupervisedSAGE.__init__(self, placeholders, features, adj, degrees, layer_infos, args)
    
    def init_aggregator(self):
        """ Initialize aggregator layers with creating reuseble convolution variables
        """
        self.aggregators = []
        for layer in range(len(self.dims) - 1):
            dim_mult = 1 if layer==0 else self.heads[layer-1]
            multihead_attns = []
            for head in range(self.heads[layer]):
                name = 'layer_' + str(layer) + '_' + str(head)
                if layer == len(self.dims) - 2:
                    aggregator = AttentionAggregator(name, dim_mult*self.dims[layer], self.dims[layer+1],
                                                     ffd_drop=self.placeholders['ffd_dropout'],
                                                     attn_drop=self.placeholders['attn_dropout'], act=lambda x:x)
                else:
                    aggregator = AttentionAggregator(name, dim_mult*self.dims[layer], self.dims[layer+1],
                                                     ffd_drop=self.placeholders['ffd_dropout'], 
                                                     attn_drop=self.placeholders['attn_dropout'])
                multihead_attns.append(aggregator)
            self.aggregators.append(multihead_attns)
    
    def aggregate(self, samples, support_sizes, batch_size):
        """ Aggregate embeddings of neighbors to compute the embeddings at next layer
        Args:
            samples: a list of node samples hops away at each layer. size=K+1
            support_sizes: a list of node numbers at each layer. size=K+1
            batch_size: input size
        Returns:
            The final embedding for input nodes
        """
        hidden = [tf.nn.embedding_lookup([self.features], node_sample) for node_sample in samples]
        num_samples = [layer_info.num_samples for layer_info in self.layer_infos] # neighbor sample size for each node
        for layer in range(len(num_samples)):
            # embedding at current layer for all support nodes hops away
            next_hidden = []
            for hop in range(len(num_samples) - layer):
                dim_mult = 1 if layer==0 else self.heads[layer-1]
                neighbor_dims = [batch_size * support_sizes[hop], 
                                     num_samples[len(num_samples) - hop - 1],
                                     dim_mult * self.dims[layer]]
                inputs = (hidden[hop], tf.reshape(hidden[hop + 1], neighbor_dims))
                attns = []
                for head in range(self.heads[layer]):
                    h = self.aggregators[layer][head](inputs)
                    attns.append(h)
                if layer == len(num_samples) - 1: # last layer
                    next_hidden.append(tf.add_n(attns) / self.heads[layer])
                else:
                    next_hidden.append(tf.concat(attns, axis=-1))
            hidden = next_hidden
        
        return hidden[0]
    
class UnsupervisedCGAT(UnsupervisedGAT):
    """
    Unsupervised GAT
    """
    def __init__(self, placeholders, features, edgetexts, vocab_dim, adj, degrees, layer_infos, args):
        self.vocab_dim = vocab_dim
        # store edgetexts:
        #    self.edge_sparse = SparseTensor, which stores the idx of edge
        #    self.edge_vecs = [edge_size, vocab_dim] tensor, which stores text for each edge
        pairs = sorted([list(k) for k in edgetexts.keys()], key=lambda x:(x[0], x[1]))
        pairs = tf.constant(pairs, dtype=tf.int64)
        idxs = tf.constant([i for i in range(len(edgetexts))])
        self.edge_idxs = tf.sparse_to_dense(pairs, [adj.shape[0], adj.shape[0]], idxs)
        self.edge_vecs = tf.constant(np.array([self.onehot(edgetexts[k], self.vocab_dim) for k in edgetexts.keys()]), dtype=tf.float32)
        UnsupervisedGAT.__init__(self, placeholders, features, adj, degrees, layer_infos, args)
        
    def onehot(self, doc, min_len):
        vec = []
        for w_idx, w_cnt in doc.items():
            for i in range(w_cnt):
                vec.append(w_idx)
        return np.bincount(np.array(vec).astype('int'), minlength=min_len)
    
    def _build(self):
        # negative sampling
        labels = tf.reshape(tf.cast(self.placeholders['batch2'], dtype=tf.int64), [self.batch_size, 1])
        self.neg_samples, _, _ = tf.nn.fixed_unigram_candidate_sampler(
            true_classes=labels,
            num_true=1,
            num_sampled=self.neg_sample_size,
            unique=False,
            range_max=len(self.degrees),
            distortion=0.75,
            unigrams=self.degrees.tolist()
        )
        
        # convolution for three set of nodes
        # sample layers of nodes
        samples1, support_sizes1, edges1 = self.sample(self.inputs1, self.batch_size)
        samples2, support_sizes2, edges2 = self.sample(self.inputs2, self.batch_size)
        neg_samples, neg_support_sizes, neg_edges = self.sample(self.neg_samples, self.neg_sample_size)
        
        # initialize the aggregators
        self.init_aggregator()
        
        # aggregate
        self.outputs1, self.vae_outs1 = self.aggregate(samples1, support_sizes1, edges1, self.batch_size)
        self.outputs2, self.vae_outs2 = self.aggregate(samples2, support_sizes2, edges2, self.batch_size)
        self.neg_outputs, self.neg_vae_outs = self.aggregate(neg_samples, neg_support_sizes, neg_edges, self.neg_sample_size)
        
        self.outputs1 = tf.nn.l2_normalize(self.outputs1, 1)
        self.outputs2 = tf.nn.l2_normalize(self.outputs2, 1)
        self.neg_outputs = tf.nn.l2_normalize(self.neg_outputs, 1)
            
    def _loss(self):
        # loss from graph reconstruction
        self.graph_loss = loss.xent_loss(self.outputs1, self.outputs2, self.neg_outputs)
        for layer in range(len(self.layer_infos) + 1):
            for var in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='aggregate/layer_' + str(layer)):
                self.graph_loss += self.weight_decay * tf.nn.l2_loss(var) 
        
        # loss from vae
        reconstr_loss1, kl_loss1 = self._loss_vae(self.vae_outs1)
        reconstr_loss2, kl_loss2 = self._loss_vae(self.vae_outs2)
        reconstr_loss_neg, kl_loss_neg = self._loss_vae(self.neg_vae_outs)
        
        # total loss
        self.reconstr_loss = reconstr_loss1 + reconstr_loss2 + reconstr_loss_neg
        self.kl_loss = kl_loss1 + kl_loss2 + kl_loss_neg
        self.loss = self.graph_loss + self.reconstr_loss + self.kl_loss
    
    def _loss_vae(self, vae_outs):
        # out = (text_vecs, x_reconstr_mean, theta, mu1, var1, z_mu0, z_var0, z_log_var0_sq)
        reconstr_losses = 0
        kl_losses = 0
        for vae_out in vae_outs:
            x, x_reconstr_mean, theta, mu1, var1, z_mu0, z_var0, z_log_var0_sq = vae_out
            topic_num = tf.cast(theta.shape[-1], dtype=tf.float32)
            # reconstruction loss
            x_reconstr_mean += 1e-10
            reconstr_loss = -tf.reduce_sum(x * tf.log(x_reconstr_mean), 1)
            # KL loss
            kl_loss = 0.5 * (tf.reduce_sum(tf.div(z_var0, var1), 1))
            kl_loss += 0.5 * (tf.reduce_sum(tf.multiply(tf.div((mu1 - z_mu0), var1), (mu1 - z_mu0)), 1))
            kl_loss -= 0.5 * topic_num 
            kl_loss += 0.5 * (tf.reduce_mean(tf.log(var1), 1) - tf.reduce_mean(z_log_var0_sq, 1))
                              
            # average over [batch_size, num_samples]
            reconstr_losses += tf.reduce_mean(tf.reduce_mean(reconstr_loss))
            kl_losses += tf.reduce_mean(tf.reduce_mean(kl_loss))
        return (reconstr_losses, kl_losses)
                
    def init_aggregator(self):
        """ Initialize aggregator layers along with vae
        """
        self.aggregators = []
        self.vaes = []
        for layer in range(len(self.dims) - 1):
            multihead_attns = []
            # gcn
            for head in range(self.heads[layer]):
                name = 'layer_' + str(layer) + '_' + str(head)
                if layer == len(self.dims) - 2:
                    aggregator = ChannelAggregator(name, self.dims[layer], self.dims[layer+1],
                                                     ffd_drop=self.placeholders['ffd_dropout'],
                                                     attn_drop=self.placeholders['attn_dropout'], act=lambda x:x)
                else:
                    aggregator = ChannelAggregator(name, self.dims[layer], self.dims[layer+1],
                                                     ffd_drop=self.placeholders['ffd_dropout'], 
                                                     attn_drop=self.placeholders['attn_dropout'])
                multihead_attns.append(aggregator)
            # vae
            vae = ChannelVAE(name, self.dims[layer], self.vocab_dim, self.heads[layer], 
                             dropout=self.placeholders['vae_dropout'])
            self.vaes.append(vae)
            self.aggregators.append(multihead_attns)
    
    def aggregate(self, samples, support_sizes, edges, batch_size):
        """ Aggregate embeddings of neighbors to compute the embeddings at next layer
        Args:
            samples: a list of node samples hops away at each layer. size=K+1
            support_sizes: a list of node numbers at each layer. size=K+1
            batch_size: input size
        Returns:
            The final embedding for input nodes
        """
        num_samples = [layer_info.num_samples for layer_info in self.layer_infos] # neighbor size for each node (size: K)
        hiddens = [tf.nn.embedding_lookup([self.features], node_sample) for node_sample in samples] # size: K+1
        vae_outs = []
        for layer in range(len(num_samples)):
            # embedding at current layer for all support nodes hops away
            next_hiddens = []
            for hop in range(len(num_samples) - layer):
                # construct edge docs
                idxs = tf.gather_nd(self.edge_idxs, edges[hop])
                docs = tf.nn.embedding_lookup(self.edge_vecs, idxs)
                # reshape docs: [batch_size, num_samples, vocab_dim]
                doc_dims = [batch_size * support_sizes[hop], 
                                     num_samples[len(num_samples) - hop - 1],
                                     self.vocab_dim]
                
                # reshape neighbor info: [batch_size, num_samples, embed_dim]
                neighbor_dims = [batch_size * support_sizes[hop], 
                                     num_samples[len(num_samples) - hop - 1],
                                     self.dims[layer]]
                
                # go through vae first
                inputs1 = (hiddens[hop], tf.reshape(hiddens[hop+1], neighbor_dims),
                          tf.reshape(docs, doc_dims))
                # out = (text_vecs, x_reconstr_mean, theta, mu1, var1, z_mu0, z_var0, z_log_var0_sq)
                vae_out = self.vaes[layer](inputs1)
                vae_outs.append(vae_out)
                channel_vecs = vae_out[2]
                
                # go through ChannelGAT 
                attns = []
                for head in range(self.heads[layer]):
                    inputs2 = (hiddens[hop], tf.reshape(hiddens[hop+1], neighbor_dims),
                           tf.slice(channel_vecs, [0,0,head], [-1,-1,1]))
                    h = self.aggregators[layer][head](inputs2)
                    attns.append(h)
                next_hiddens.append(tf.add_n(attns) / self.heads[layer])
                
            hiddens = next_hiddens
        
        return (hiddens[0], vae_outs)
    

        