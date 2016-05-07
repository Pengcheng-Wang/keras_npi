#!/usr/bin/env python
# coding: utf-8
import os

import numpy as np
from keras.engine.topology import Merge, Input, InputLayer
from keras.engine.training import Model
from keras.layers.core import Dense, Activation, Reshape
from keras.layers.embeddings import Embedding
from keras.layers.recurrent import LSTM
from keras.models import Sequential, model_from_yaml
from keras.regularizers import l1, l2
from keras.utils.visualize_util import plot
import keras.backend as K

from npi.add.config import FIELD_ROW, FIELD_DEPTH, PROGRAM_VEC_SIZE, MAX_PROGRAM_NUM, PROGRAM_KEY_VEC_SIZE, FIELD_WIDTH
from npi.add.lib import AdditionProgramSet, AdditionEnv, run_npi
from npi.core import NPIStep, Program, IntegerArguments, StepOutput, RuntimeSystem, PG_RETURN, StepInOut, StepInput
from npi.terminal_core import TerminalNPIRunner

__author__ = 'k_morishita'


class AdditionNPIModel(NPIStep):
    model = None

    def __init__(self, system: RuntimeSystem, model_path: str=None, program_set: AdditionProgramSet=None):
        self.system = system
        self.model_path = model_path
        self.program_set = program_set
        self.batch_size = 1
        self.build()
        self.load_weights()

    def build(self):
        L1_COST = 0.001
        L2_COST = 0.001
        enc_size = self.size_of_env_observation()
        argument_size = IntegerArguments.size_of_arguments
        input_enc = InputLayer(batch_input_shape=(self.batch_size, enc_size), name='input_enc')
        input_arg = InputLayer(batch_input_shape=(self.batch_size, argument_size), name='input_arg')
        input_prg = Embedding(input_dim=MAX_PROGRAM_NUM, output_dim=PROGRAM_VEC_SIZE, input_length=1,
                              W_regularizer=l1(l=L1_COST),
                              batch_input_shape=(self.batch_size, 1))

        f_enc = Sequential(name='f_enc')
        f_enc.add(Merge([input_enc, input_arg], mode='concat'))
        # f_enc.add(Dense(20, W_regularizer=l1(l=L1_COST)))
        f_enc.add(Reshape((1, enc_size + argument_size)))

        program_embedding = Sequential(name='program_embedding')
        program_embedding.add(input_prg)

        f_lstm = Sequential(name='f_lstm')
        f_lstm.add(Merge([f_enc, program_embedding], mode='concat'))
        f_lstm.add(Activation('relu', name='relu_lstm_0'))
        # f_lstm.add(LSTM(64, return_sequences=True, stateful=True, W_regularizer=l2(l=L2_COST)))
        # f_lstm.add(Activation('relu', name='relu_lstm_1'))
        f_lstm.add(LSTM(64, return_sequences=False, stateful=True, W_regularizer=l2(l=L2_COST)))
        f_lstm.add(Activation('relu', name='relu_lstm_2'))
        # plot(f_lstm, to_file='f_lstm.png', show_shapes=True)

        f_end = Sequential(name='f_end')
        f_end.add(f_lstm)
        f_end.add(Dense(10, W_regularizer=l1(l=L1_COST)))
        f_end.add(Dense(1, W_regularizer=l1(l=L1_COST)))
        f_end.add(Activation('sigmoid', name='sigmoid_end'))
        # plot(f_end, to_file='f_end.png', show_shapes=True)

        f_prog = Sequential(name='f_prog')
        f_prog.add(f_lstm)
        f_prog.add(Dense(PROGRAM_KEY_VEC_SIZE, W_regularizer=l1(l=L1_COST)))
        f_prog.add(Dense(PROGRAM_VEC_SIZE, W_regularizer=l1(l=L1_COST)))
        f_prog.add(Activation('softmax', name='softmax_prog'))
        # plot(f_prog, to_file='f_prog.png', show_shapes=True)

        f_arg = Sequential(name='f_arg')
        f_arg.add(f_lstm)
        f_arg.add(Dense(20, W_regularizer=l1(l=L1_COST)))
        f_arg.add(Dense(argument_size, W_regularizer=l1(l=L1_COST)))
        f_arg.add(Activation('relu', name='relu_arg'))
        # plot(f_arg, to_file='f_arg.png', show_shapes=True)

        model = Model([input_enc.input, input_arg.input, input_prg.input],
                      [f_end.output, f_prog.output, f_arg.output],
                      name="npi")
        model.compile(optimizer='rmsprop',
                      loss=['binary_crossentropy', 'categorical_crossentropy', 'mean_squared_error'],
                      loss_weights=[1.0, 0.2, 1.0])
        plot(model, to_file='model.png', show_shapes=True)

        self.model = model

    def reset(self):
        super(AdditionNPIModel, self).reset()
        for l in self.model.layers:
            if type(l) is LSTM:
                l.reset_states()

    def fit(self, steps_list, epoch=100):
        """

        :param int epoch:
        :param typing.List[typing.Dict[q=dict, steps=typing.List[StepInOut]]] steps_list:
        :return:
        """

        addition_env = AdditionEnv(FIELD_ROW, FIELD_WIDTH, FIELD_DEPTH)
        npi_runner = TerminalNPIRunner(None, self)

        for ep in range(1, epoch+1):
            for idx, steps_dict in enumerate(steps_list):
                question = steps_dict['q']
                steps = steps_dict['steps']
                xs = []
                ys = []
                for step in steps:
                    # INPUT
                    xs.append(self.convert_input(step.input))
                    # OUTPUT
                    o = step.output
                    y = [np.array((o.r, ))]
                    if o.program:
                        y += [o.program.to_one_hot(PROGRAM_VEC_SIZE), o.arguments.values]
                    else:
                        y += [np.zeros((PROGRAM_VEC_SIZE, )), IntegerArguments().values]
                    y = [yy.reshape((self.batch_size, -1)) for yy in y]
                    ys.append(y)

                it = 0
                while True:
                    it += 1
                    self.reset()
                    losses = []

                    for i, (x, y) in enumerate(zip(xs, ys)):
                        loss = self.model.train_on_batch(x, y)
                        losses.append(loss)
                    print("ep: %2d %s %s: ave loss %.3f" % (ep, idx, it, np.average(losses)))

                    if it % 100 == 0:
                        self.save()
                        print("save model")
                        addition_env.reset()
                        self.reset()
                        try:
                            run_npi(addition_env, npi_runner, self.program_set.ADD, question)
                            print(question)
                            if question['correct']:
                                break
                        except StopIteration:
                            pass

                if idx % 10 == 0:
                    self.save()
                    print("save model")

    def convert_input(self, params: StepInput):
        x_pg = np.array((params.program.program_id,))
        x = [xx.reshape((self.batch_size, -1)) for xx in (params.env, params.arguments.values, x_pg)]
        return x

    def step(self, env_observation: np.ndarray, pg: Program, arguments: IntegerArguments) -> StepOutput:
        x = self.convert_input(StepInput(env_observation, pg, arguments))
        r, pg_one_hot, args_value = self.model.predict(x, batch_size=1)  # if batch_size==1, returns single row
        program = self.program_set.get(pg_one_hot.argmax())
        ret = StepOutput(r, program, IntegerArguments(values=args_value))
        return ret

    def save(self):
        self.model.save_weights(self.model_path, overwrite=True)

    def load_weights(self):
        if os.path.exists(self.model_path):
            self.model.load_weights(self.model_path)

    @staticmethod
    def size_of_env_observation():
        return FIELD_ROW * FIELD_DEPTH
