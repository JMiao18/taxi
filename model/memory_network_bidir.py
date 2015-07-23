from theano import tensor

from blocks.bricks import application, MLP, Rectifier, Initializable, Softmax, Linear
from blocks.bricks.parallel import Fork
from blocks.bricks.recurrent import Bidirectional, LSTM

import data
from data import transformers
from data.cut import TaxiTimeCutScheme
from data.hdf5 import TaxiDataset, TaxiStream
import error
from model import ContextEmbedder

from memory_network import StreamRecurrent as Stream
from memory_network import MemoryNetworkBase

class RecurrentEncoder(Initializable):
    def __init__(self, config, output_dim, activation, **kwargs):
        super(RecurrentEncoder, self).__init__(**kwargs)

        self.config = config
        self.context_embedder = ContextEmbedder(config)

        self.rec = Bidirectional(LSTM(dim=config.rec_state_dim, name='encoder_recurrent'))
        self.fork = Fork(
                [name for name in self.rec.prototype.apply.sequences
                      if name != 'mask'],
                prototype=Linear())

        rto_in = config.rec_state_dim * 2 + sum(x[2] for x in config.dim_embeddings)
        self.rec_to_output = MLP(
                    activations=[Rectifier() for _ in config.dim_hidden] + [activation],
                    dims=[rto_in] + config.dim_hidden + [output_dim],
                    name='encoder_rto')

        self.children = [self.context_embedder, self.rec, self.fork, self.rec_to_output]

        self.rec_inputs = ['latitude', 'longitude', 'latitude_mask']
        self.inputs = self.context_embedder.inputs + self.rec_inputs

    def _push_allocation_config(self):
        self.fork.input_dim = 2
        self.fork.output_dims = [ self.rec.children[0].get_dim(name) 
                                  for name in self.fork.output_names ]

    def _push_initialization_config(self):
        for brick in self.children:
            brick.weights_init = self.config.weights_init
            brick.biases_init = self.config.biases_init

    @application
    def apply(self, latitude, longitude, latitude_mask, **kwargs):
        latitude = (latitude.T - data.train_gps_mean[0]) / data.train_gps_std[0]
        longitude = (longitude.T - data.train_gps_mean[1]) / data.train_gps_std[1]
        latitude_mask = latitude_mask.T

        rec_in = tensor.concatenate((latitude[:, :, None], longitude[:, :, None]),
                                    axis=2)
        path = self.rec.apply(self.fork.apply(rec_in), mask=latitude_mask)[0]

        last_id = tensor.cast(latitude_mask.sum(axis=0) - 1, dtype='int64')
        
        path_representation = (path[0][:, -self.config.rec_state_dim:],
                path[last_id - 1, tensor.arange(last_id.shape[0])]
                    [:, :self.config.rec_state_dim])

        embeddings = tuple(self.context_embedder.apply(
                            **{k: kwargs[k] for k in self.context_embedder.inputs }))

        inputs = tensor.concatenate(path_representation + embeddings, axis=1)
        outputs = self.rec_to_output.apply(inputs)

        return outputs


class Model(MemoryNetworkBase):
    def __init__(self, config, **kwargs):
        super(Model, self).__init__(config, **kwargs)

        # Build prefix encoder : recurrent then MLP
        self.prefix_encoder = RecurrentEncoder(self.config.prefix_encoder,
                                               self.config.representation_size,
                                               self.config.representation_activation(),
                                               name='prefix_encoder')

        # Build candidate encoder
        self.candidate_encoder = RecurrentEncoder(self.config.candidate_encoder,
                                                  self.config.representation_size,
                                                  self.config.representation_activation(),
                                                  name='candidate_encoder')

        # Rest of the stuff
        self.softmax = Softmax()

        self.inputs = self.prefix_encoder.inputs \
                      + ['candidate_'+k for k in self.candidate_encoder.inputs]

        self.children = [ self.prefix_encoder,
                          self.candidate_encoder,
                          self.softmax ]


    @application(outputs=['destination'])
    def predict(self, **kwargs):
        prefix_representation = self.prefix_encoder.apply(
                **{ name: kwargs[name] for name in self.prefix_encoder.inputs })

        candidate_representation = self.prefix_encoder.apply(
                **{ name: kwargs['candidate_'+name] for name in self.candidate_encoder.inputs })

        if self.config.normalize_representation:
            candidate_representation = candidate_representation \
                    / tensor.sqrt((candidate_representation ** 2).sum(axis=1, keepdims=True))

        similarity_score = tensor.dot(prefix_representation, candidate_representation.T)
        similarity = self.softmax.apply(similarity_score)

        candidate_mask = kwargs['candidate_latitude_mask']
        candidate_last = tensor.cast(candidate_mask.sum(axis=1) - 1, 'int64')
        candidate_destination = tensor.concatenate(
                (kwargs['candidate_latitude'][tensor.arange(candidate_mask.shape[0]), candidate_last]
                                             [:, None],
                 kwargs['candidate_longitude'][tensor.arange(candidate_mask.shape[0]), candidate_last]
                                              [:, None]),
                axis=1)

        return tensor.dot(similarity, candidate_destination)

    @predict.property('inputs')
    def predict_inputs(self):
        return self.inputs

    @application(outputs=['cost'])
    def cost(self, **kwargs):
        y_hat = self.predict(**kwargs)
        y = tensor.concatenate((kwargs['destination_latitude'][:, None],
                                kwargs['destination_longitude'][:, None]), axis=1)

        return error.erdist(y_hat, y).mean()

    @cost.property('inputs')
    def cost_inputs(self):
        return self.inputs + ['destination_latitude', 'destination_longitude']