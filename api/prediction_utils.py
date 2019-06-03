"""
Utility functions for audio loading, model loading, and making predictions
"""

import pandas as pd
import numpy as np
import tensorflow as tf
import vggish.vggish_input
import vggish.vggish_params
import vggish.vggish_postprocess
import vggish.vggish_slim
from pydub import AudioSegment
from pathlib import Path

import tensorflow.keras as keras
from tensorflow.keras.models import Model
from tensorflow.keras.layers import (Input, Dense, BatchNormalization, Dropout, Lambda,
                          Activation, Concatenate)
import tensorflow.keras.backend as K


def audio_load(filename):
    """ Load audio file using pydub w/ ffmpeg and returns a numpy array with sample rate
    Requires FFMPEG to be installed on system

    Args:
        filename: path to input audio file in any format that's readable by ffmpeg
    Returns:
        a 2-tuple: (numpy array of waveform, native sample rate of the audio)
    """
    song = AudioSegment.from_file(Path(filename))
    songwave = np.array(song.get_array_of_samples()).reshape(-1, song.channels)/ song.max_possible_amplitude
    return songwave, song.frame_rate

def load_checkpoint(checkpointfile):
    """ Load checkpoint file for vggish

    Args:
        checkpointfile: path to tf checkpoint file of the pretrained vggish model
    Returns:
        a TF session containing restored weights of vggish
    """
    # tf.reset_default_graph()
    tf.Graph().as_default()
    sess = tf.Session()
    vggish.vggish_slim.define_vggish_slim(training=False)
    vggish.vggish_slim.load_vggish_slim_checkpoint(sess, checkpointfile)
    return sess

def feature_extraction(songwave, pca_params, session, sample_rate):
    """ Applying VGGish to extract features and do post processing (PCA and discretization)

    Args:
        songwave: waveform loaded from audio file
        pca_params: pca coefficients from vggish
        session: TF session containing restored weights of VGGish
        sample_rate: sample rate of the waveform
    Returns:
        feature embedding in numpy array of shape (number of seconds of audio, 128)
    """
    examples_batch = vggish.vggish_input.waveform_to_examples(songwave, sample_rate)
    pproc = vggish.vggish_postprocess.Postprocessor(str(pca_params))
    features_tensor = session.graph.get_tensor_by_name(vggish.vggish_params.INPUT_TENSOR_NAME)
    embedding_tensor = session.graph.get_tensor_by_name(vggish.vggish_params.OUTPUT_TENSOR_NAME)
    [embedding_batch] = session.run([embedding_tensor],feed_dict={features_tensor: examples_batch})
    postprocessed_batch = pproc.postprocess(embedding_batch)
    return postprocessed_batch


def block(vggish_features, window, hop):
    """ Expanding a 2D array to 3D blocks by rolling window of extracted features

    Args:
        vggish_features: 2D numpy array
        window: window length, ie size of blocks
        hop: hop lengh, determines the size of new dim and how much to overlap among blocks
    Returns:
        a 3D numpy array of overlapping blocks of the original 2D array
    """
    index_window = np.arange(window)
    # index_hop = np.arange(0,vggish_features.shape[0] - hop - (10 - vggish_features.shape[0]%window) - 1, hop)[:, np.newaxis]
    index_hop = np.arange(0,vggish_features.shape[0] - window, hop)[:, np.newaxis]
    rolling_block = np.take(vggish_features,index_window + index_hop, axis=0)
    return rolling_block

def constrct_model(pathname, weights_name):
    """ Model loading function for last layers, contains attention pooling
    
    Args:
        pathname: folder name of saved weights
        weights_name: file name of saved weights
    Returns:
        model loaded with saved weights, ready for prediction
    """
    def attention_pooling(inputs, **kwargs):
        [out, att] = inputs

        epsilon = 1e-7
        att = K.clip(att, epsilon, 1. - epsilon)
        normalized_att = att / K.sum(att, axis=1)[:, None, :]

        return K.sum(out * normalized_att, axis=1)


    def pooling_shape(input_shape):

        if isinstance(input_shape, list):
            (sample_num, time_steps, freq_bins) = input_shape[0]

        else:
            (sample_num, time_steps, freq_bins) = input_shape

        return (sample_num, freq_bins)

    time_steps = 10
    freq_bins = 128
    classes_num = 527

    # Hyper parameters
    hidden_units = 1024
    drop_rate = 0.5
    batch_size = 500

    # Embedded layers
    input_layer = Input(shape=(time_steps, freq_bins))

    a1 = Dense(hidden_units)(input_layer)
    a1 = BatchNormalization()(a1)
    a1 = Activation('relu')(a1)
    a1 = Dropout(drop_rate)(a1)

    a2 = Dense(hidden_units)(a1)
    a2 = BatchNormalization()(a2)
    a2 = Activation('relu')(a2)
    a2 = Dropout(drop_rate)(a2)

    a3 = Dense(hidden_units)(a2)
    a3 = BatchNormalization()(a3)
    a3 = Activation('relu')(a3)
    a3 = Dropout(drop_rate)(a3)

    # Multi-level Attention Model
    cla1 = Dense(classes_num, activation='sigmoid')(a2)
    att1 = Dense(classes_num, activation='softmax')(a2)
    out1 = Lambda(
        attention_pooling, output_shape=pooling_shape)([cla1, att1])

    cla2 = Dense(classes_num, activation='sigmoid')(a3)
    att2 = Dense(classes_num, activation='softmax')(a3)
    out2 = Lambda(
        attention_pooling, output_shape=pooling_shape)([cla2, att2])

    b1 = Concatenate(axis=-1)([out1, out2])
    b1 = Dense(classes_num)(b1)
    output_layer = Activation('sigmoid')(b1)

    model_graph = Model(inputs=input_layer, outputs=output_layer)
    model_graph.load_weights(pathname + weights_name)
    return model_graph


def model_prediction(model, block_10s, threshold=0.2):
    """ Forward pass of the second part of the layer, predicting probs for each label

    Args:
        pathname: path to folder of saved Keras model
        weights_name: name of saved Keras model wights
        block_10s: 3D numpy array from rolled and post processed vggish embeddings
        normsize: normalization of vggish embeddings
        threshold: probability threshold for predicting labels
    Returns:
        2D numpy array of shape ((window/hop) * num of seconds, num of labels) where
        each element is the probability of the corresponding label at corresponding time
    """
    prediction = model.predict((np.float32(block_10s)-128.)/128.)
    find_label_index = np.where(prediction > threshold)
    time_index = find_label_index[0]
    predicted_label = find_label_index[1]
    top_preds = []
    for i in range(block_10s.shape[0]):
        idx = (i == time_index)
        top_preds.append((i, list(predicted_label[idx])))
    return top_preds

def prediction_label(pathname, labels_file, labels_col, preds,):
    """ Translating numeric labels to text

    Args:
        pathname: folder path of csv file containing table of label number and texts
        labels_file: name of csv file containing table of label number and texts
        labels_col: name of column containing text labels
        preds: input list of predictions from model, indexed by time
    Returns:
        dict of predicted text labels
    """
    df = pd.read_csv(Path(pathname)/ labels_file)
    series = df[labels_col]
    # labels = [(x, list(series[y])) for x, y in preds]
    labels = {x: list(series[y]) for x, y in preds}
    return labels
