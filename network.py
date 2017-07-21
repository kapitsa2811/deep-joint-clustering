'''
Created on Jul 11, 2017

@author: yawarnihal, eliealjalbout
'''

from datetime import datetime
import logging

from lasagne import layers
import lasagne
from lasagne.layers.helper import get_all_layers
from sklearn.cluster.k_means_ import KMeans
import theano

from misc import getClusterMetricString
import numpy as np
import theano.tensor as T
from misc import evaluateKMeans


logFormatter = logging.Formatter("[%(asctime)s]  %(message)s", datefmt='%m/%d %I:%M:%S')

rootLogger = logging.getLogger()
rootLogger.setLevel(logging.DEBUG)

fileHandler = logging.FileHandler(datetime.now().strftime('dcjc_%H_%M_%d_%m.log'))
fileHandler.setFormatter(logFormatter)
rootLogger.addHandler(fileHandler)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(logFormatter)
rootLogger.addHandler(consoleHandler)

class Unpool2DLayer(layers.Layer):
    """
    This layer performs unpooling over the last two dimensions
    of a 4D tensor.
    """
    def __init__(self, incoming, ds, **kwargs):
        super(Unpool2DLayer, self).__init__(incoming, **kwargs)
        self.ds = ds
    
    def get_output_shape_for(self, input_shape):
        output_shape = list(input_shape)
        output_shape[2] = input_shape[2] * self.ds[0]
        output_shape[3] = input_shape[3] * self.ds[1]
        return tuple(output_shape)

    def get_output_for(self, incoming, **kwargs):
        ds = self.ds
        return incoming.repeat(ds[0], axis=2).repeat(ds[1], axis=3)

class ClusteringLayer(layers.Layer):

    def __init__(self, incoming, num_clusters, initial_clusters, num_samples, latent_space_dim, **kwargs):
        super(ClusteringLayer, self).__init__(incoming, **kwargs)
        self.num_clusters = num_clusters
        self.W = self.add_param(theano.shared(initial_clusters), initial_clusters.shape, 'W')
        self.num_samples = num_samples
        self.latent_space_dim = latent_space_dim
        
    def get_output_shape_for(self, input_shape):
        return (input_shape[0], self.num_clusters)
    
    def get_output_for(self, incoming, **kwargs):
        z_expanded = incoming.reshape((self.num_samples, 1, self.latent_space_dim))
        z_expanded = T.tile(z_expanded, (1, self.num_clusters, 1)) 
        u_expanded = T.tile(self.W, (self.num_samples, 1, 1))
        distances_from_cluster_centers = (z_expanded - u_expanded).norm(2, axis=2)
        qij_numerator = 1 + distances_from_cluster_centers * distances_from_cluster_centers
        qij_numerator = 1 / qij_numerator
        normalizer_q = qij_numerator.sum(axis=1).reshape((self.num_samples, 1))
        return qij_numerator / normalizer_q;
        
invertible_layers = [layers.Conv2DLayer, layers.MaxPool2DLayer]

class DCJC(object):
    
    def __init__(self, network_description):
        self.name = network_description['name']
        self.setNetworkTypeBasedOnName()
        if (self.network_type == 'AE'):
            self.input_var = T.matrix('input_var')
            self.target_var = T.matrix('target_var')
        else:
            self.input_var = T.tensor4('input_var')
            self.target_var = T.tensor4('target_var')
        self.network = self.getNetworkExpression(network_description)
        rootLogger.info("Network: "+self.networkToStr())
        recon_prediction_expression = layers.get_output(self.network)
        encode_prediction_expression = layers.get_output(self.encode_layer, deterministic=True)
        loss = self.getReconstructionLossExpression(recon_prediction_expression, self.target_var)
        params = lasagne.layers.get_all_params(self.network, trainable=True)
        updates = lasagne.updates.adam(loss, params)
        self.trainAutoencoder = theano.function([self.input_var, self.target_var], loss, updates=updates) 
        self.predictReconstruction = theano.function([self.input_var], recon_prediction_expression) 
        self.predictEncoding = theano.function([self.input_var], encode_prediction_expression)
    
    
    def pretrainWithData(self, dataset, pretrain_epochs):
        batch_size = 250
        Z = np.zeros((dataset.train_input.shape[0], self.encode_size), dtype=np.float32);
        train_set = 'train'
        if self.network_type == 'AE':
            train_set = 'train_flat'
        for epoch in range(pretrain_epochs):
            pretrain_error = 0
            pretrain_total_batches = 0
            for batch in dataset.iterate_minibatches(train_set, batch_size, shuffle=True):
                inputs, targets = batch
                pretrain_error += self.trainAutoencoder(inputs, targets)
                pretrain_total_batches += 1
            # REMOVE THIS - JUST FOR DEBUGGING#
            ###############
            if epoch%1 == 0:
                for i, batch in enumerate(dataset.iterate_minibatches(train_set, batch_size, shuffle=False)):
                    Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])
                rootLogger.info(evaluateKMeans(Z, dataset.train_labels, "%d/%d [%.4f]"%(epoch + 1, pretrain_epochs, pretrain_error / pretrain_total_batches)))
            else:
                rootLogger.info("%-30s     %8s     %8s"%("%d/%d [%.4f]"%(epoch + 1, pretrain_epochs, pretrain_error / pretrain_total_batches),"",""))
            ###############
        
        for i, batch in enumerate(dataset.iterate_minibatches(train_set, batch_size, shuffle=False)):
            Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])           
        np.save('models/z_%s.npy' % self.name, Z)
        np.savez('models/m_%s.npz' % self.name, *lasagne.layers.get_all_param_values(self.network))
        np.savetxt('models/z_%s.csv' % self.name, Z, delimiter=",")
    
    def doClustering(self, dataset, complete_loss, cluster_train_epochs, repeats):
        P = T.matrix('P')
        batch_size = 100
        with np.load('models/m_%s.npz' % self.name) as f:
            param_values = [f['arr_%d' % i] for i in range(len(f.files))]
        lasagne.layers.set_all_param_values(self.network, param_values)
        Z = np.load('models/z_%s.npy' % self.name)
        kmeans = KMeans(init='k-means++', n_clusters=10)
        kmeans.fit(Z)
        cluster_centers = kmeans.cluster_centers_
        rootLogger.info(getClusterMetricString('Initial', dataset.train_labels, kmeans.labels_))
        
        dec_network = ClusteringLayer(self.encode_layer, 10, cluster_centers, batch_size, self.encode_size)
        
        clustering_loss = self.getClusteringLossExpression(layers.get_output(dec_network), P)
        reconstruction_loss = self.getReconstructionLossExpression(layers.get_output(self.network), self.target_var)
        params_ae = lasagne.layers.get_all_params(self.network, trainable=True)
        params_dec = lasagne.layers.get_all_params(dec_network, trainable=True)
        
        w_cluster_loss = 1
        
        total_loss = clustering_loss
        if (complete_loss):
            total_loss = w_cluster_loss * total_loss + reconstruction_loss 
            total_loss *= 0.3
        all_params = params_dec
        if complete_loss:
            all_params.extend(params_ae)
        all_params = list(set(all_params))

        updates = lasagne.updates.adam(total_loss, all_params)
        
        getSoftAssignments = theano.function([self.input_var], layers.get_output(dec_network))
        
        trainFunction = None
        if complete_loss:
            trainFunction = theano.function([self.input_var, self.target_var, P], total_loss, updates=updates)
        else:
            trainFunction = theano.function([self.input_var, P], clustering_loss, updates=updates)
        
        train_set = 'train'
        if self.network_type == 'AE':
            train_set = 'train_flat'
        
        for _iter in range(repeats):
            qij = np.zeros((dataset.train_input.shape[0], 10), dtype=np.float32)
            for i, batch in enumerate(dataset.iterate_minibatches(train_set, batch_size, shuffle=False)):
                qij[i * batch_size: (i + 1) * batch_size] = getSoftAssignments(batch[0])
            pij = self.calculateP(qij) 
            
            for epoch in range(cluster_train_epochs):
                cluster_train_error = 0
                cluster_train_total_batches = 0
                for i, batch in enumerate(dataset.iterate_minibatches(train_set, batch_size, shuffle=False)):
                    if (complete_loss):
                        cluster_train_error += trainFunction(batch[0], batch[0], pij[i * batch_size:(i + 1) * batch_size])
                    else:
                        cluster_train_error += trainFunction(batch[0], pij[i * batch_size:(i + 1) * batch_size])
                    cluster_train_total_batches += 1
            rootLogger.info('Done with epoch %d/%d [TE: %.4f]' % (epoch + 1, cluster_train_epochs, cluster_train_error / cluster_train_total_batches))
            
            Z = np.zeros((dataset.train_input.shape[0], self.encode_size), dtype=np.float32);
            for i, batch in enumerate(dataset.iterate_minibatches(train_set, batch_size, shuffle=False)):
                Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])
        
            kmeans = KMeans(init='k-means++', n_clusters=10)
            kmeans.fit(Z)
            cluster_centers = kmeans.cluster_centers_
            rootLogger.info(getClusterMetricString('Iteration-%d' % _iter, dataset.train_labels, kmeans.labels_))

    def calculateP(self, Q):
        f = Q.sum(axis=0)
        pij_numerator = Q * Q
        pij_numerator = pij_numerator / f;
        normalizer_p = pij_numerator.sum(axis=1).reshape((Q.shape[0], 1))
        P = pij_numerator / normalizer_p
        return P
    
    def getClusteringLossExpression(self, Q_expression, P_expression):
        log_arg = P_expression / Q_expression
        log_exp = T.log(log_arg)
        sum_arg = P_expression * log_exp
        loss = sum_arg.sum(axis=1).sum(axis=0)
        return loss
    
    def getReconstructionLossExpression(self, prediction_expression, target_var):
        loss = lasagne.objectives.squared_error(prediction_expression, target_var)
        loss = loss.mean()
        return loss

    # Functions for network construction based on architecture dictionary
        
    def getNonLinearity(self, non_linearity_name):
        return {
                'rectify': lasagne.nonlinearities.rectify,
                'linear': lasagne.nonlinearities.linear,
                'elu': lasagne.nonlinearities.elu
                }[non_linearity_name]
    
    def setNetworkTypeBasedOnName(self):
        if (self.name.split('_')[0].split('-')[0] == 'fc'):
            self.network_type = 'AE'
        else:
            self.network_type = 'CAE'
            
    def getLayer(self, network, layer_definition, is_encode_layer=False, is_last_layer=False):
        if (layer_definition['layer_type'] == 'Dense'):
            if is_last_layer:
                return layers.DenseLayer(network, num_units=layer_definition['num_units'], nonlinearity=lasagne.nonlinearities.linear, name='fc[{}]'.format(layer_definition['num_units']))
            else:
                return layers.DenseLayer(network, num_units=layer_definition['num_units'], nonlinearity=lasagne.nonlinearities.rectify, name='fc[{}]'.format(layer_definition['num_units']))
        if (layer_definition['layer_type'] == 'Conv2D'):
            network = layers.Conv2DLayer(network, num_filters=layer_definition['num_filters'], pad='same', filter_size=(layer_definition['filter_size'][0], layer_definition['filter_size'][1]), nonlinearity=self.getNonLinearity(layer_definition['non_linearity']), W=lasagne.init.GlorotUniform(), name='{}[{}]'.format(layer_definition['num_filters'], 'x'.join([str(fs) for fs in layer_definition['filter_size']])))
            if (is_encode_layer):
                self.encode_layer = lasagne.layers.flatten(network, name='fl')
                self.encode_size = layer_definition['output_shape'][0] * layer_definition['output_shape'][1] * layer_definition['output_shape'][2]
            return network
        elif (layer_definition['layer_type'] == 'MaxPool2D'):
            network = lasagne.layers.MaxPool2DLayer(network, pool_size=(layer_definition['filter_size'][0], layer_definition['filter_size'][1]), name='max[{}]'.format('x'.join([str(fs) for fs in layer_definition['filter_size']])))
            if (is_encode_layer):
                self.encode_layer = lasagne.layers.flatten(network, name='fl')
                self.encode_size = layer_definition['output_shape'][0] * layer_definition['output_shape'][1] * layer_definition['output_shape'][2]
            return network
        elif (layer_definition['layer_type'] == 'Encode'):
            if self.network_type == 'CAE':
                network = lasagne.layers.flatten(network, name='fl')
                network = lasagne.layers.DenseLayer(network, num_units=layer_definition['encode_size'], nonlinearity=self.getNonLinearity(layer_definition['non_linearity']), W=lasagne.init.GlorotUniform(), name='fc[{}]'.format(layer_definition['encode_size']))
                network = lasagne.layers.GaussianNoiseLayer(network, 0.15, name='noi')
                self.encode_layer = network
                self.encode_size = layer_definition['encode_size']
                network = lasagne.layers.DenseLayer(network, num_units=layer_definition['output_shape'][0] * layer_definition['output_shape'][1] * layer_definition['output_shape'][2], nonlinearity=self.getNonLinearity(layer_definition['non_linearity']), W=lasagne.init.GlorotUniform(), name='fc[{}]'.format(layer_definition['output_shape'][0] * layer_definition['output_shape'][1] * layer_definition['output_shape'][2]))
                return lasagne.layers.reshape(network, (-1, layer_definition['output_shape'][0], layer_definition['output_shape'][1], layer_definition['output_shape'][2]), name='rs')
            else:
                network = layers.DenseLayer(network, num_units=layer_definition['num_units'], nonlinearity=self.getNonLinearity(layer_definition['non_linearity']), name='fc[{}]'.format(layer_definition['num_units']))
                self.encode_layer = network
                self.encode_size = layer_definition['num_units']
                return network
        elif (layer_definition['layer_type'] == 'Unpool2D'):
            return Unpool2DLayer(network, (layer_definition['filter_size'][0], layer_definition['filter_size'][1]), name=' unp[{}]'.format(str(layer_definition['filter_size'][0]) + 'x' + str(layer_definition['filter_size'][1])))
        elif (layer_definition['layer_type'] == 'Deconv2D'):
            return layers.Deconv2DLayer(network, crop='same', num_filters=layer_definition['num_filters'], filter_size=(layer_definition['filter_size'][0], layer_definition['filter_size'][1]), nonlinearity=self.getNonLinearity(layer_definition['non_linearity']), W=lasagne.init.GlorotUniform(), name='{}[{}]'.format(layer_definition['num_filters'], 'x'.join([str(fs) for fs in layer_definition['filter_size']])))
        elif (layer_definition['layer_type'] == 'Input'):
            if self.network_type == 'CAE':
                return layers.InputLayer(shape=(None, layer_definition['output_shape'][0], layer_definition['output_shape'][1], layer_definition['output_shape'][2]), input_var=self.input_var)
            else:
                return layers.InputLayer(shape=(None, layer_definition['output_shape'][2]), input_var=self.input_var)
    
    def populateNetworkOutputShapes(self, network_description):
        last_layer_dimensions = network_description['layers_encode'][0]['output_shape']
        for layer in network_description['layers_encode']:
            if (layer['layer_type'] == 'MaxPool2D'):
                layer['output_shape'] = [last_layer_dimensions[0], last_layer_dimensions[1] / layer['filter_size'][0], last_layer_dimensions[2] / layer['filter_size'][1]]
            elif (layer['layer_type'] == 'Conv2D'):
                layer['output_shape'] = [layer['num_filters'], last_layer_dimensions[1] - (layer['filter_size'][0] + 1)*0 , last_layer_dimensions[2] - (layer['filter_size'][1] + 1)*0]
            elif (layer['layer_type'] == 'Encode'):
                if self.network_type == 'CAE':
                    layer['output_shape'] = last_layer_dimensions
                else:
                    layer['output_shape'] = [1, 1, layer['num_units']] 
            elif (layer['layer_type'] == 'Dense'):
                layer['output_shape'] = [1, 1, layer['num_units']] 
            last_layer_dimensions = layer['output_shape']

    def populateMirroredNetwork(self, network_description):
        network_description['layers_decode'] = [] 
        old_network_description = network_description.copy()
        for i in range(len(old_network_description['layers_encode']) - 1, -1, -1):
            if (old_network_description['layers_encode'][i]['layer_type'] == 'MaxPool2D'):
                old_network_description['layers_decode'].append({
                                                             'layer_type':'Unpool2D',
                                                             'filter_size':old_network_description['layers_encode'][i]['filter_size']
                                                             })
            elif(old_network_description['layers_encode'][i]['layer_type'] == 'Conv2D'):
                network_description['layers_decode'].append({
                                                             'layer_type':'Deconv2D',
                                                             'non_linearity': old_network_description['layers_encode'][i]['non_linearity'],
                                                             'filter_size':old_network_description['layers_encode'][i]['filter_size'],
                                                             'num_filters':old_network_description['layers_encode'][i - 1]['output_shape'][0]
                                                             })
            elif(old_network_description['layers_encode'][i]['layer_type'] == 'Dense' or (old_network_description['layers_encode'][i]['layer_type'] == 'Encode' and self.network_type == 'AE')):
                network_description['layers_decode'].append({
                                                             'layer_type':'Dense',
                                                             'num_units':old_network_description['layers_encode'][i - 1]['output_shape'][2]
                                                             })
                
        
    def getNetworkExpression(self, network_description):
        network = None
        self.populateNetworkOutputShapes(network_description)
        for i, layer in enumerate(network_description['layers_encode']):
            network = self.getLayer(network, layer, i == len(network_description['layers_encode']) - 1)
            
        layer_list = get_all_layers(network)
        if network_description['use_inverse_layers'] == True:
            for i in range(len(layer_list) - 1, 0, -1):
                if any(type(layer_list[i]) is invertible_layer for invertible_layer in invertible_layers):
                    network = lasagne.layers.InverseLayer(network, layer_list[i], name='Inv[' + layer_list[i].name + ']')
            #network = lasagne.layers.NonlinearityLayer(network, lasagne.nonlinearities.sigmoid, name='smd')
        else:
            self.populateMirroredNetwork(network_description)
            for i, layer in enumerate(network_description['layers_decode']):
                network = self.getLayer(network, layer, False, i == len(network_description['layers_decode']) - 1)
        return network

   
    def networkToStr(self):
        layers = lasagne.layers.get_all_layers(self.network)
        result = ''
        for layer in layers:
            t = type(layer)
            if t is lasagne.layers.input.InputLayer:
                pass
            else:
                result += ' ' + layer.name
        return result.strip()
