'''
Created on Jul 11, 2017

@author: yawarnihal, eliealjalbout
'''

from datetime import datetime
import logging

from lasagne import layers
import lasagne
from lasagne.layers.helper import get_all_layers
import theano

from customlayers import ClusteringLayer, Unpool2DLayer, getSoftAssignments
from misc import evaluateKMeans, visualizeData, rescaleReshapeAndSaveImage
import numpy as np
import theano.tensor as T

from lasagne.layers import batch_norm

logFormatter = logging.Formatter("[%(asctime)s]  %(message)s", datefmt='%m/%d %I:%M:%S')

rootLogger = logging.getLogger()
rootLogger.setLevel(logging.DEBUG)

fileHandler = logging.FileHandler(datetime.now().strftime('logs/dcjc_%H_%M_%d_%m.log'))
fileHandler.setFormatter(logFormatter)
rootLogger.addHandler(fileHandler)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(logFormatter)
rootLogger.addHandler(consoleHandler)


class DCJC(object):
    def __init__(self, network_description):

        self.name = network_description['name']
        netbuilder = NetworkBuilder(network_description)
        self.network = netbuilder.buildNetwork()
        self.encode_layer, self.encode_size = netbuilder.getEncodeLayerAndSize()
        self.t_input, self.t_target = netbuilder.getInputAndTargetVars()
        self.input_type = netbuilder.getInputType()
        self.batch_size = netbuilder.getBatchSize()
        rootLogger.info("Network: " + self.networkToStr())
        recon_prediction_expression = layers.get_output(self.network)
        encode_prediction_expression = layers.get_output(self.encode_layer, deterministic=True)
        loss = self.getReconstructionLossExpression(recon_prediction_expression, self.t_target)
        weightsl2 = lasagne.regularization.regularize_network_params(self.network, lasagne.regularization.l2)
        loss += (5e-5 * weightsl2)
        params = lasagne.layers.get_all_params(self.network, trainable=True)
        self.learning_rate = theano.shared(lasagne.utils.floatX(0.01))
        updates = lasagne.updates.nesterov_momentum(loss, params, learning_rate=self.learning_rate)
        # updates = lasagne.updates.adam(loss, params)
        self.trainAutoencoder = theano.function([self.t_input, self.t_target], loss, updates=updates)
        self.predictReconstruction = theano.function([self.t_input], recon_prediction_expression)
        self.predictEncoding = theano.function([self.t_input], encode_prediction_expression)

    def pretrainWithData(self, dataset, pretrain_epochs, continue_training=False):
        batch_size = self.batch_size
        Z = np.zeros((dataset.input.shape[0], self.encode_size), dtype=np.float32);
        if continue_training:
            with np.load('saved_params/%s/m_%s.npz' % (dataset.name, self.name)) as f:
                param_values = [f['arr_%d' % i] for i in range(len(f.files))]
                lasagne.layers.set_all_param_values(self.network, param_values, trainable=True)
        for epoch in range(pretrain_epochs):
            pretrain_error = 0
            pretrain_total_batches = 0
            for batch in dataset.iterate_minibatches(self.input_type, batch_size, shuffle=True):
                inputs, targets = batch
                pretrain_error += self.trainAutoencoder(inputs, targets)
                pretrain_total_batches += 1
            self.learning_rate.set_value(self.learning_rate.get_value() * lasagne.utils.floatX(0.9999))
            if (epoch + 1) % 1 == 0:
                for i, batch in enumerate(dataset.iterate_minibatches(self.input_type, batch_size, shuffle=False)):
                    Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])
                    '''
                    for i, x in enumerate(self.predictReconstruction(batch[0])):
                        rescaleReshapeAndSaveImage(x[0], "dumps/%02d%03d.jpg"%(epoch,i));
                    '''
                rootLogger.info(evaluateKMeans(Z, dataset.labels, dataset.getClusterCount(), "%d/%d [%.4f]" % (
                    epoch + 1, pretrain_epochs, pretrain_error / pretrain_total_batches))[0])
            else:
                rootLogger.info("%-30s     %8s     %8s" % (
                    "%d/%d [%.4f]" % (epoch + 1, pretrain_epochs, pretrain_error / pretrain_total_batches), "", ""))

        for i, batch in enumerate(dataset.iterate_minibatches(self.input_type, batch_size, shuffle=False)):
            Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])

        np.save('saved_params/%s/z_%s.npy' % (dataset.name, self.name), Z)
        np.savez('saved_params/%s/m_%s.npz' % (dataset.name, self.name),
                 *lasagne.layers.get_all_param_values(self.network, trainable=True))

    def doClusteringWithKMeansLoss(self, dataset, epochs):
        batch_size = self.batch_size

        Z = np.load('saved_params/%s/z_%s.npy' % (dataset.name, self.name))
        quality_desc, cluster_centers = evaluateKMeans(Z, dataset.labels, dataset.getClusterCount(), 'Initial')
        rootLogger.info(quality_desc)

        with np.load('saved_params/%s/m_%s.npz' % (dataset.name, self.name)) as f:
            param_values = [f['arr_%d' % i] for i in range(len(f.files))]
            lasagne.layers.set_all_param_values(self.network, param_values, trainable=True)

        reconstruction_loss = self.getReconstructionLossExpression(layers.get_output(self.network), self.t_target)
        t_cluster_centers = theano.shared(cluster_centers)
        kmeansLoss = self.getKMeansLoss(layers.get_output(self.encode_layer), t_cluster_centers,
                                        dataset.getClusterCount(),
                                        self.encode_size, batch_size)
        params = lasagne.layers.get_all_params(self.network, trainable=True)
        weight_reconstruction = 1
        weight_kmeans = 0.05
        total_loss = weight_kmeans * kmeansLoss + weight_reconstruction * reconstruction_loss
        updates = lasagne.updates.nesterov_momentum(total_loss, params, learning_rate=0.01)
        # updates = lasagne.updates.adam(loss, params)
        trainKMeansWithAE = theano.function([self.t_input, self.t_target], total_loss, updates=updates)

        for epoch in range(epochs):
            error = 0
            total_batches = 0
            for batch in dataset.iterate_minibatches(self.input_type, batch_size, shuffle=True):
                inputs, targets = batch
                error += trainKMeansWithAE(inputs, targets)
                total_batches += 1
            if (epoch + 1) % 1 == 0:
                for i, batch in enumerate(dataset.iterate_minibatches(self.input_type, batch_size, shuffle=False)):
                    Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])
                quality_desc, cluster_centers = evaluateKMeans(Z, dataset.labels, dataset.getClusterCount(),
                                                               "%d/%d [%.4f]" % (
                                                                   epoch + 1, epochs, error / total_batches))
                rootLogger.info(quality_desc)
                t_cluster_centers.set_value(cluster_centers)
            else:
                rootLogger.info("%-30s     %8s     %8s" % (
                    "%d/%d [%.4f]" % (epoch + 1, epochs, error / total_batches), "", ""))

    # load pretrained models, then either train with DEC loss jointly with reconstruction or alone
    def doClusteringWithKLdivLoss(self, dataset, combined_loss, epochs):
        P = T.matrix('P')
        batch_size = self.batch_size
        with np.load('saved_params/%s/m_%s.npz' % (dataset.name, self.name)) as f:
            param_values = [f['arr_%d' % i] for i in range(len(f.files))]
            lasagne.layers.set_all_param_values(self.network, param_values, trainable=True)
        Z = np.load('saved_params/%s/z_%s.npy' % (dataset.name, self.name))
        quality_desc, cluster_centers = evaluateKMeans(Z, dataset.labels, dataset.getClusterCount(), 'Initial')
        rootLogger.info(quality_desc)
        dec_network = ClusteringLayer(self.encode_layer, dataset.getClusterCount(), cluster_centers, batch_size,
                                      self.encode_size)
        dec_output_exp = layers.get_output(dec_network)
        encode_output_exp = layers.get_output(self.network)
        clustering_loss = self.getClusteringLossExpression(dec_output_exp, P)
        reconstruction_loss = self.getReconstructionLossExpression(encode_output_exp, self.t_target)
        params_ae = lasagne.layers.get_all_params(self.network, trainable=True)
        params_dec = lasagne.layers.get_all_params(dec_network, trainable=True)

        w_cluster_loss = 1
        w_reconstruction_loss = 1
        total_loss = w_cluster_loss * clustering_loss
        if (combined_loss):
            total_loss = total_loss + w_reconstruction_loss * reconstruction_loss
        all_params = params_dec
        if combined_loss:
            all_params.extend(params_ae)
        all_params = list(set(all_params))

        #updates = lasagne.updates.adam(total_loss, all_params)
        updates = lasagne.updates.nesterov_momentum(total_loss, all_params, learning_rate=0.01)

        getSoftAssignments = theano.function([self.t_input], dec_output_exp)

        trainFunction = None
        if combined_loss:
            trainFunction = theano.function([self.t_input, self.t_target, P], total_loss, updates=updates)
        else:
            trainFunction = theano.function([self.t_input, P], clustering_loss, updates=updates)

        for epoch in range(epochs):
            qij = np.zeros((dataset.input.shape[0], dataset.getClusterCount()), dtype=np.float32)
            for i, batch in enumerate(dataset.iterate_minibatches(self.input_type, batch_size, shuffle=False)):
                qij[i * batch_size: (i + 1) * batch_size] = getSoftAssignments(batch[0])
            # np.save('saved_params/%s/q_%s.npy' % (dataset.name, 'Test'), qij)
            pij = self.calculateP(qij)
            # np.save('saved_params/%s/p_%s.npy' % (dataset.name, 'Test'), pij)

            error = 0
            total_batches = 0
            for i, batch in enumerate(dataset.iterate_minibatches(self.input_type, batch_size, pij, shuffle=True)):
                if (combined_loss):
                    error += trainFunction(batch[0], batch[0], batch[1])
                else:
                    error += trainFunction(batch[0], batch[1])
                total_batches += 1

            for i, batch in enumerate(dataset.iterate_minibatches(self.input_type, batch_size, shuffle=False)):
                Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])

            # np.save('saved_params/%s/z_%s.npy' % (dataset.name, 'Test'), Z)
            rootLogger.info(evaluateKMeans(Z, dataset.labels, dataset.getClusterCount(), "%d [%.4f]" % (
                epoch, error / total_batches))[0])

        for i, batch in enumerate(dataset.iterate_minibatches(self.input_type, batch_size, shuffle=False)):
            Z[i * batch_size:(i + 1) * batch_size] = self.predictEncoding(batch[0])

        np.save('saved_params/%s/pc_z_%s.npy' % (dataset.name, self.name), Z)
        np.savez('saved_params/%s/pc_m_%s.npz' % (dataset.name, self.name),
                 *lasagne.layers.get_all_param_values(self.network, trainable=True))

    def calculateP(self, Q):
        f = Q.sum(axis=0)
        pij_numerator = Q * Q
        pij_numerator = pij_numerator / f
        normalizer_p = pij_numerator.sum(axis=1).reshape((Q.shape[0], 1))
        P = pij_numerator / normalizer_p
        return P

    def getClusteringLossExpression(self, Q_expression, P_expression):
        log_arg = P_expression / Q_expression
        log_exp = T.log(log_arg)
        sum_arg = P_expression * log_exp
        loss = sum_arg.sum(axis=1).sum(axis=0)
        return loss

    def getReconstructionLossExpression(self, prediction_expression, t_target):
        loss = lasagne.objectives.squared_error(prediction_expression, t_target)
        loss = loss.mean()
        return loss

    def getKMeansLoss(self, latent_space_expression, t_cluster_centers, num_clusters, latent_space_dim, num_samples, soft_loss = True):
        t_soft_assignments = getSoftAssignments(latent_space_expression, t_cluster_centers, num_clusters,
                                                latent_space_dim, num_samples)
        z = latent_space_expression.reshape((num_samples, 1, latent_space_dim))
        z = T.tile(z, (1, num_clusters, 1))
        u = t_cluster_centers.reshape((1, num_clusters, latent_space_dim))
        u = T.tile(u, (num_samples, 1, 1))
        distances = (z - u).norm(2, axis=2).reshape((num_samples, num_clusters))
        if soft_loss:
            weighted_distances = distances * t_soft_assignments
            loss = weighted_distances.sum(axis=1).mean()
        else:
            loss = distances.min(axis=1).mean()
        return loss

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


class NetworkBuilder(object):
    def __init__(self, network_description):
        self.network_description = self.populateMissingDescriptions(network_description)
        if self.network_description['network_type'] == 'CAE':
            self.t_input = T.tensor4('input_var')
            self.t_target = T.tensor4('target_var')
            self.input_type = "IMAGE"
        else:
            self.t_input = T.matrix('input_var')
            self.t_target = T.matrix('target_var')
            self.input_type = "FLAT"
        self.network_type = self.network_description['network_type']
        self.batch_norm = bool(self.network_description["use_batch_norm"])
        self.layer_list = []

    def getBatchSize(self):
        return self.network_description["batch_size"]

    def getInputAndTargetVars(self):
        return self.t_input, self.t_target

    def getInputType(self):
        return self.input_type

    def buildNetwork(self):
        network = None
        for layer in self.network_description['layers']:
            network = self.processLayer(network, layer)
        return network

    def getEncodeLayerAndSize(self):
        return self.encode_layer, self.encode_size

    def populateDecoder(self, encode_layers):
        decode_layers = []
        for i, layer in reversed(list(enumerate(encode_layers))):
            if (layer["type"] == "MaxPool2D*"):
                decode_layers.append({
                    "type": "InverseMaxPool2D",
                    "layer_index": i,
                    'filter_size': layer['filter_size']
                })
            elif (layer["type"] == "MaxPool2D"):
                decode_layers.append({
                    "type": "Unpool2D",
                    'filter_size': layer['filter_size']
                })
            elif (layer["type"] == "Conv2D"):
                decode_layers.append({
                    'type': 'Deconv2D',
                    'conv_mode': layer['conv_mode'],
                    'non_linearity': layer['non_linearity'],
                    'filter_size': layer['filter_size'],
                    'num_filters': encode_layers[i - 1]['output_shape'][0]
                })
            elif (layer["type"] == "Dense" and not layer["is_encode"]):
                decode_layers.append({
                    'type': 'Dense',
                    'num_units': encode_layers[i]['output_shape'][2],
                    'non_linearity': encode_layers[i]['non_linearity']
                })
                if (encode_layers[i - 1]['type'] in ("Conv2D", "MaxPool2D", "MaxPool2D*")):
                    decode_layers.append({
                        "type": "Reshape",
                        "output_shape": encode_layers[i - 1]['output_shape']
                    })
        encode_layers.extend(decode_layers)

    def populateShapes(self, layers):
        last_layer_dimensions = layers[0]['output_shape']
        for layer in layers[1:]:
            if (layer['type'] == 'MaxPool2D' or layer['type'] == 'MaxPool2D*'):
                layer['output_shape'] = [last_layer_dimensions[0], last_layer_dimensions[1] / layer['filter_size'][0],
                                         last_layer_dimensions[2] / layer['filter_size'][1]]
            elif (layer['type'] == 'Conv2D'):
                multiplier = 1
                if (layer['conv_mode'] == "same"):
                    multiplier = 0
                layer['output_shape'] = [layer['num_filters'],
                                         last_layer_dimensions[1] - (layer['filter_size'][0] - 1) * multiplier,
                                         last_layer_dimensions[2] - (layer['filter_size'][1] - 1) * multiplier]
            elif (layer['type'] == 'Dense'):
                layer['output_shape'] = [1, 1, layer['num_units']]
            last_layer_dimensions = layer['output_shape']

    def populateMissingDescriptions(self, network_description):
        for layer in network_description['layers']:
            if 'conv_mode' not in layer:
                layer['conv_mode'] = 'valid'
            layer['is_encode'] = False
        network_description['layers'][-1]['is_encode'] = True
        if 'output_non_linearity' not in network_description:
            network_description['output_non_linearity'] = network_description['layers'][1]['non_linearity']
        self.populateShapes(network_description['layers'])
        self.populateDecoder(network_description['layers'])
        if 'use_batch_norm' not in network_description:
            network_description['use_batch_norm'] = False
        if 'network_type' not in network_description:
            if (network_description['name'].split('_')[0].split('-')[0] == 'fc'):
                network_description['network_type'] = 'AE'
            else:
                network_description['network_type'] = 'CAE'
        for layer in network_description['layers']:
            if 'is_encode' not in layer:
                layer['is_encode'] = False
            layer['is_output'] = False
        network_description['layers'][-1]['is_output'] = True
        network_description['layers'][-1]['non_linearity'] = network_description['output_non_linearity']
        return network_description

    def processLayer(self, network, layer_definition):
        if (layer_definition["type"] == "Input"):
            if self.network_type == 'CAE':
                network = lasagne.layers.InputLayer(
                    shape=tuple([None] + layer_definition['output_shape']), input_var=self.t_input)
            elif self.network_type == 'AE':
                network = lasagne.layers.InputLayer(
                    shape=(None, layer_definition['output_shape'][2]), input_var=self.t_input)
        elif (layer_definition['type'] == 'Dense'):
            network = lasagne.layers.DenseLayer(network, num_units=layer_definition['num_units'],
                                                nonlinearity=self.getNonLinearity(layer_definition['non_linearity']),
                                                name=self.getLayerName(layer_definition))
        elif (layer_definition['type'] == 'Conv2D'):
            network = lasagne.layers.Conv2DLayer(network, num_filters=layer_definition['num_filters'],
                                                 filter_size=tuple(layer_definition["filter_size"]),
                                                 pad=layer_definition['conv_mode'],
                                                 nonlinearity=self.getNonLinearity(layer_definition['non_linearity']),
                                                 name=self.getLayerName(layer_definition))
        elif (layer_definition['type'] == 'MaxPool2D' or layer_definition['type'] == 'MaxPool2D*'):
            network = lasagne.layers.MaxPool2DLayer(network, pool_size=tuple(layer_definition["filter_size"]),
                                                    name=self.getLayerName(layer_definition))
        elif (layer_definition['type'] == 'InverseMaxPool2D'):
            network = lasagne.layers.InverseLayer(network, self.layer_list[layer_definition['layer_index']],
                                                  name=self.getLayerName(layer_definition))
        elif (layer_definition['type'] == 'Unpool2D'):
            network = Unpool2DLayer(network, tuple(layer_definition['filter_size']),
                                    name=self.getLayerName(layer_definition))
        elif (layer_definition['type'] == 'Reshape'):
            network = lasagne.layers.ReshapeLayer(network,
                                                  shape=tuple([-1] + layer_definition["output_shape"]),
                                                  name=self.getLayerName(layer_definition))
        elif (layer_definition['type'] == 'Deconv2D'):
            network = lasagne.layers.Deconv2DLayer(network, num_filters=layer_definition['num_filters'],
                                                   filter_size=tuple(layer_definition['filter_size']),
                                                   crop=layer_definition['conv_mode'],
                                                   nonlinearity=self.getNonLinearity(layer_definition['non_linearity']),
                                                   name=self.getLayerName(layer_definition))

        self.layer_list.append(network)

        if (self.batch_norm and (not layer_definition["is_output"]) and layer_definition['type'] in (
                "Conv2D", "Deconv2D")):
            network = batch_norm(network)

        if (layer_definition['is_encode']):
            self.encode_layer = lasagne.layers.flatten(network, name='fl')
            self.encode_size = layer_definition['output_shape'][0] * layer_definition['output_shape'][1] * \
                               layer_definition['output_shape'][2]
        return network

    def getLayerName(self, layer_definition):
        if (layer_definition['type'] == 'Dense'):
            return 'fc[{}]'.format(layer_definition['num_units'])
        elif (layer_definition['type'] == 'Conv2D'):
            return '{}[{}]'.format(layer_definition['num_filters'],
                                   'x'.join([str(fs) for fs in layer_definition['filter_size']]))
        elif (layer_definition['type'] == 'MaxPool2D' or layer_definition['type'] == 'MaxPool2D*'):
            return 'max[{}]'.format('x'.join([str(fs) for fs in layer_definition['filter_size']]))
        elif (layer_definition['type'] == 'InverseMaxPool2D'):
            return 'ups*[{}]'.format('x'.join([str(fs) for fs in layer_definition['filter_size']]))
        elif (layer_definition['type'] == 'Unpool2D'):
            return 'ups[{}]'.format(
                str(layer_definition['filter_size'][0]) + 'x' + str(layer_definition['filter_size'][1]))
        elif (layer_definition['type'] == 'Deconv2D'):
            return '{}[{}]'.format(layer_definition['num_filters'],
                                   'x'.join([str(fs) for fs in layer_definition['filter_size']]))
        elif (layer_definition['type'] == 'Reshape'):
            return "rsh"

    def getNonLinearity(self, non_linearity):
        return {
            'rectify': lasagne.nonlinearities.rectify,
            'linear': lasagne.nonlinearities.linear,
            'elu': lasagne.nonlinearities.elu
        }[non_linearity]
