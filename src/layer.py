import tensorflow as tf
import numpy as np

def uniform_init(shape, scale=0.05):
    return tf.random_uniform(shape, minval=-scale, maxval=scale, dtype=tf.float32)

def glorot_init(shape):
    init_range = np.sqrt(6.0 / (shape[0] + shape[1]))
    return tf.random_uniform(shape, minval=-init_range, maxval=init_range, dtype=tf.float32)

def zeros_init(shape):
    return tf.zeros(shape, dtype=tf.float32)

def ones_init(shape):
    return tf.ones(shape, dtype=tf.float32)

    
class ChannelAggregator(object):
    """ 
    one channel aggregator for CGAT
    """
    def __init__(self, name, input_dim, output_dim,
                 ffd_drop=0., attn_drop=0., usebias=False, act=tf.nn.elu):
        self.name = name
        self.ffd_drop = ffd_drop
        self.attn_drop = attn_drop
        self.usebias = usebias
        self.act = act
        
        with tf.variable_scope(name) as scope:
            self.conv1 = tf.layers.Conv1D(filters=output_dim, kernel_size=1, name='conv1')
            self.conv2 = tf.layers.Conv1D(filters=1, kernel_size=1, name='conv2')
            if usebias:
                self.bias = tf.get_variable('bias',
                                            initializer=zero_init((output_dim)))
   
    def __call__(self, inputs):
        """
        Args:
            input: (self_vecs, neighbor_vecs, channel_vecs)
            self_vecs.shape = [batch_size, dim]
            neighbor_vecs.shape = [batch_size, num_samples, dim]
            channel_vecs.shape = [batch_size, num_samples, 1]
        """
        self_vecs, neighbor_vecs, channel_vecs = inputs
        # reshape: [batch_size, 1, dim]; then concatenate: [batch_size, 1+num_samples, dim]
        vecs = tf.concat([tf.expand_dims(self_vecs, axis=1), neighbor_vecs], axis=1)
        # dropout
        vecs = tf.nn.dropout(vecs, 1-self.ffd_drop)
        # transform and self attention
        with tf.variable_scope(self.name) as scope:
            vecs_trans = self.conv1(vecs) # [batch_size, 1+num_samples, output_dim]
            f_1 = self.conv2(vecs_trans)  # [batch_size, 1+num_samples, 1]
            f_2 = self.conv2(vecs_trans)
            logits = f_1 + tf.transpose(f_2, [0, 2, 1]) # [batch_size, 1+num_samples, 1+num_samples]
            coefs = tf.nn.softmax(tf.nn.leaky_relu(logits))
            # only maintain the target node for each batch
            coefs = tf.slice(coefs, [0,0,0], [-1,1,-1]) # [batch_size, 1, 1+num_samples]
            # channel (add one dim for self channel)
            self_channel = tf.slice(tf.ones_like(coefs), [0,0,0], [-1,1,1]) # [batch_size, 1, 1]
            channels = tf.concat((self_channel, channel_vecs), axis=1) # [batch_size, 1+num_samples, 1]
            channels = tf.transpose(channels, [0, 2, 1]) # [batch_size, 1, 1+num_samples]
            # channel * attention
            coefs = tf.multiply(channels, coefs)
            # dropout
            coefs = tf.nn.dropout(coefs, 1-self.attn_drop)
            vecs_trans = tf.nn.dropout(vecs_trans, 1-self.ffd_drop)
            # aggregate
            output = tf.matmul(coefs, vecs_trans) # [batch_size, 1, output_dim]
            output = tf.squeeze(output) # [batch_size, output_dim]
            if self.usebias:
                output += self.bias
        return self.act(output)    
            
class ChannelVAE(object):
    def __init__(self, name, embed_dim, vocab_dim, channel_dim, dropout=0., act=tf.nn.softplus):
        # input_dim: vocabulary size; output_dim: topic number
        self.name = name
        self.dropout = dropout
        self.act = act
        self.channel_dim = channel_dim
        
        self.vars = {}
        with tf.variable_scope(name) as scope:
            self.vars['encoder'] = {'phi': tf.get_variable('phi', shape=[embed_dim, channel_dim]),
                                    'sigma': tf.get_variable('sigma', shape=[1, channel_dim]),
                                    'h1_weights': tf.get_variable('h1_weights', shape=[vocab_dim, 100]),
                                    'h1_bias': tf.get_variable('h1_bias', initializer=zeros_init((100))),
                                    'h2_weights': tf.get_variable('h2_weights', shape=[100, 100]),
                                    'h2_bias': tf.get_variable('h2_bias', initializer=zeros_init((100))),
                                    'mean_weights': tf.get_variable('mean_weights', shape=[100, channel_dim]),
                                    'mean_bias': tf.get_variable('mean_bias', initializer=zeros_init((channel_dim))),
                                    'sigma_weights': tf.get_variable('sigma_weights', shape=[100, channel_dim]),
                                    'sigma_bias': tf.get_variable('sigma_bias', initializer=zeros_init((channel_dim)))}
            self.vars['decoder'] = {'beta': tf.get_variable('beta', initializer=glorot_init((channel_dim, vocab_dim)))}
    
    def __call__(self, inputs):
        """
        Args:
            input: (self_vecs, neighbor_vecs, text_vecs)
            self_vecs.shape = [batch_size, embed_dim]
            neighbor_vecs.shape = [batch_size, num_samples, embed_dim]
            text_vecs.shape = [batch_size, num_samples, vocab_dim]
        """
        self_vecs, neighbor_vecs, text_vecs = inputs
        # construct h_{i}+h_{j}: [batch_size, num_samples, embed_dim]
        sum_vecs = tf.multiply(tf.expand_dims(self_vecs, axis=1), neighbor_vecs)
        
        # prior
        a = tf.exp(tf.nn.softmax(tf.matmul(sum_vecs, self.vars['encoder']['phi']))) # [batch_size, num_samples, output_dim]
        mu1 = tf.log(a) - tf.expand_dims(tf.reduce_mean(tf.log(a), 2), 2)
        var1 = (1.0 / a) * (1. - (2.0 / self.channel_dim)) + \
                 (1.0 / (self.channel_dim * self.channel_dim)) * tf.expand_dims(tf.reduce_sum(1.0 / a, 2), 2)
        mu1 = tf.nn.softmax(mu1)
        var1 = tf.nn.softmax(var1)
        
        # encoder network
        layer1 = self.act(tf.add(tf.matmul(text_vecs, self.vars['encoder']['h1_weights']),
                                self.vars['encoder']['h1_bias']))
        layer2 = self.act(tf.add(tf.matmul(layer1, self.vars['encoder']['h2_weights']),
                                self.vars['encoder']['h2_bias']))
        layer_do = tf.nn.dropout(layer2, 1.0-self.dropout)
        # shape: [batch_size, num_samples, output_dim]
        z_mu0 = tf.contrib.layers.batch_norm(tf.add(tf.matmul(layer_do, self.vars['encoder']['mean_weights']),
                                                    self.vars['encoder']['mean_bias']))
        z_log_var0_sq = tf.contrib.layers.batch_norm(tf.add(tf.matmul(layer_do, self.vars['encoder']['sigma_weights']),
                                                            self.vars['encoder']['sigma_bias']))
        z_mu0 = tf.nn.softmax(z_mu0)
        z_log_var0_sq = tf.log(tf.nn.softmax(z_log_var0_sq))
        
        # reparameterization trick
        eps = tf.random_normal(shape=(1, self.channel_dim), mean=0., stddev=1., dtype=tf.float32)
        z = tf.add(z_mu0, tf.multiply(tf.sqrt(tf.exp(z_log_var0_sq)), eps))
        z_var0 = tf.exp(z_log_var0_sq)
        
        # decoder network
        theta = tf.nn.dropout(tf.nn.softmax(z), 1.0-self.dropout)
        beta = tf.nn.softmax(tf.contrib.layers.batch_norm(self.vars['decoder']['beta']))
        x_reconstr_mean = tf.add(tf.matmul(theta, beta), 0.0)

        return (text_vecs, x_reconstr_mean, theta, mu1, var1, z_mu0, z_var0, z_log_var0_sq)





