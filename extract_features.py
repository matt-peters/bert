# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Extract pre-computed feature vectors from BERT."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import re

import modeling
import tokenization
import tensorflow as tf

import h5py
import numpy as np
from tqdm import tqdm

flags = tf.flags

FLAGS = flags.FLAGS

flags.DEFINE_string("input_file", None, "")

flags.DEFINE_string("output_file", None, "")

flags.DEFINE_string("output_ids_file", None, "")

flags.DEFINE_string(
    "bert_config_file", None,
    "The config json file corresponding to the pre-trained BERT model. "
    "This specifies the model architecture.")

flags.DEFINE_integer(
    "max_seq_length", 128,
    "The maximum total input sequence length after WordPiece tokenization. "
    "Sequences longer than this will be truncated, and sequences shorter "
    "than this will be padded.")

flags.DEFINE_string(
    "init_checkpoint", None,
    "Initial checkpoint (usually from a pre-trained BERT model).")

flags.DEFINE_string("vocab_file", None,
                    "The vocabulary file that the BERT model was trained on.")

flags.DEFINE_bool(
    "do_lower_case", True,
    "Whether to lower case the input text. Should be True for uncased "
    "models and False for cased models.")

flags.DEFINE_integer("batch_size", 32, "Batch size for predictions.")

flags.DEFINE_bool("use_tpu", False, "Whether to use TPU or GPU/CPU.")

flags.DEFINE_string("master", None,
                    "If using a TPU, the address of the master.")

flags.DEFINE_integer(
    "num_tpu_cores", 8,
    "Only used if `use_tpu` is True. Total number of TPU cores to use.")

flags.DEFINE_bool(
    "use_one_hot_embeddings", False,
    "If True, tf.one_hot will be used for embedding lookups, otherwise "
    "tf.nn.embedding_lookup will be used. On TPUs, this should be True "
    "since it is much faster.")

flags.DEFINE_bool(
    "do_tokens_only", True, "Only output features for first byte pair of each token")

NUM_BERT_LAYERS = 0

class InputExample(object):

  def __init__(self, unique_id, text_a, text_b):
    self.unique_id = unique_id
    self.text_a = text_a
    self.text_b = text_b


class InputFeatures(object):
  """A single set of features of data."""

  def __init__(self, unique_id, tokens, input_ids, input_mask, input_type_ids):
    self.unique_id = unique_id
    self.tokens = tokens
    self.input_ids = input_ids
    self.input_mask = input_mask
    self.input_type_ids = input_type_ids


def input_fn_builder(features, seq_length):
  """Creates an `input_fn` closure to be passed to TPUEstimator."""

  all_unique_ids = []
  all_input_ids = []
  all_input_mask = []
  all_input_type_ids = []

  for feature in features:
    all_unique_ids.append(feature.unique_id)
    all_input_ids.append(feature.input_ids)
    all_input_mask.append(feature.input_mask)
    all_input_type_ids.append(feature.input_type_ids)

  def input_fn(params):
    """The actual input function."""
    batch_size = params["batch_size"]

    num_examples = len(features)

    # This is for demo purposes and does NOT scale to large data sets. We do
    # not use Dataset.from_generator() because that uses tf.py_func which is
    # not TPU compatible. The right way to load data is with TFRecordReader.
    d = tf.data.Dataset.from_tensor_slices({
        "unique_ids":
            tf.constant(all_unique_ids, shape=[num_examples], dtype=tf.int32),
        "input_ids":
            tf.constant(
                all_input_ids, shape=[num_examples, seq_length],
                dtype=tf.int32),
        "input_mask":
            tf.constant(
                all_input_mask,
                shape=[num_examples, seq_length],
                dtype=tf.int32),
        "input_type_ids":
            tf.constant(
                all_input_type_ids,
                shape=[num_examples, seq_length],
                dtype=tf.int32),
    })

    d = d.batch(batch_size=batch_size, drop_remainder=False)
    return d

  return input_fn


def model_fn_builder(bert_config, init_checkpoint, use_tpu,
                     use_one_hot_embeddings):
  """Returns `model_fn` closure for TPUEstimator."""

  def model_fn(features, labels, mode, params):  # pylint: disable=unused-argument
    """The `model_fn` for TPUEstimator."""

    unique_ids = features["unique_ids"]
    input_ids = features["input_ids"]
    input_mask = features["input_mask"]
    input_type_ids = features["input_type_ids"]

    model = modeling.BertModel(
        config=bert_config,
        is_training=False,
        input_ids=input_ids,
        input_mask=input_mask,
        token_type_ids=input_type_ids,
        use_one_hot_embeddings=use_one_hot_embeddings)

    if mode != tf.estimator.ModeKeys.PREDICT:
      raise ValueError("Only PREDICT modes are supported: %s" % (mode))

    tvars = tf.trainable_variables()
    scaffold_fn = None
    (assignment_map, _) = modeling.get_assignment_map_from_checkpoint(
        tvars, init_checkpoint)
    if use_tpu:

      def tpu_scaffold():
        tf.train.init_from_checkpoint(init_checkpoint, assignment_map)
        return tf.train.Scaffold()

      scaffold_fn = tpu_scaffold
    else:
      tf.train.init_from_checkpoint(init_checkpoint, assignment_map)

    all_layers = model.get_all_encoder_layers()
    # Prepend the embeddings to all_layers
    all_layers.insert(0, model.get_embedding_output())

    predictions = {
        "unique_id": unique_ids,
    }

    global NUM_BERT_LAYERS
    NUM_BERT_LAYERS = len(all_layers)
    for layer_index in range(NUM_BERT_LAYERS):
      predictions["layer_output_%d" % layer_index] = all_layers[layer_index]

    output_spec = tf.contrib.tpu.TPUEstimatorSpec(
        mode=mode, predictions=predictions, scaffold_fn=scaffold_fn)
    return output_spec

  return model_fn


def convert_examples_to_features(examples, seq_length, tokenizer):
  """Loads a data file into a list of `InputBatch`s."""

  features = []
  for (ex_index, example) in enumerate(examples):
    # Split words independently to maintain alignment with labels
    tokens_a = []
    tokenized_text_a = example.text_a.split(" ")
    for text_a_token in tokenized_text_a:
      tokens_a.extend(tokenizer.tokenize(text_a_token))

    tokens_b = None
    if example.text_b:
      tokens_b = tokenizer.tokenize(example.text_b)

    if tokens_b:
      # Modifies `tokens_a` and `tokens_b` in place so that the total
      # length is less than the specified length.
      # Account for [CLS], [SEP], [SEP] with "- 3"
      _truncate_seq_pair(tokens_a, tokens_b, seq_length - 3)
    else:
      # Account for [CLS] and [SEP] with "- 2"
      if len(tokens_a) > seq_length - 2:
        tokens_a = tokens_a[0:(seq_length - 2)]

    # The convention in BERT is:
    # (a) For sequence pairs:
    #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
    #  type_ids: 0     0  0    0    0     0       0 0     1  1  1  1   1 1
    # (b) For single sequences:
    #  tokens:   [CLS] the dog is hairy . [SEP]
    #  type_ids: 0     0   0   0  0     0 0
    #
    # Where "type_ids" are used to indicate whether this is the first
    # sequence or the second sequence. The embedding vectors for `type=0` and
    # `type=1` were learned during pre-training and are added to the wordpiece
    # embedding vector (and position vector). This is not *strictly* necessary
    # since the [SEP] token unambiguously separates the sequences, but it makes
    # it easier for the model to learn the concept of sequences.
    #
    # For classification tasks, the first vector (corresponding to [CLS]) is
    # used as as the "sentence vector". Note that this only makes sense because
    # the entire model is fine-tuned.
    tokens = []
    input_type_ids = []
    tokens.append("[CLS]")
    input_type_ids.append(0)
    for token in tokens_a:
      tokens.append(token)
      input_type_ids.append(0)
    tokens.append("[SEP]")
    input_type_ids.append(0)

    if tokens_b:
      for token in tokens_b:
        tokens.append(token)
        input_type_ids.append(1)
      tokens.append("[SEP]")
      input_type_ids.append(1)

    input_ids = tokenizer.convert_tokens_to_ids(tokens)

    # The mask has 1 for real tokens and 0 for padding tokens. Only real
    # tokens are attended to.
    input_mask = [1] * len(input_ids)

    # Zero-pad up to the sequence length.
    while len(input_ids) < seq_length:
      input_ids.append(0)
      input_mask.append(0)
      input_type_ids.append(0)

    assert len(input_ids) == seq_length
    assert len(input_mask) == seq_length
    assert len(input_type_ids) == seq_length

    if ex_index < 5:
      tf.logging.info("*** Example ***")
      tf.logging.info("unique_id: %s" % (example.unique_id))
      tf.logging.info("tokens: %s" % " ".join([str(x) for x in tokens]))
      tf.logging.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
      tf.logging.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
      tf.logging.info(
          "input_type_ids: %s" % " ".join([str(x) for x in input_type_ids]))

    features.append(
        InputFeatures(
            unique_id=example.unique_id,
            tokens=tokens,
            input_ids=input_ids,
            input_mask=input_mask,
            input_type_ids=input_type_ids))
  return features


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
  """Truncates a sequence pair in place to the maximum length."""

  # This is a simple heuristic which will always truncate the longer sequence
  # one token at a time. This makes more sense than truncating an equal percent
  # of tokens from each, since if one sequence is very short then each token
  # that's truncated likely contains more information than a longer sequence.
  while True:
    total_length = len(tokens_a) + len(tokens_b)
    if total_length <= max_length:
      break
    if len(tokens_a) > len(tokens_b):
      tokens_a.pop()
    else:
      tokens_b.pop()


def read_examples(input_file):
  """Read a list of `InputExample`s from an input file."""
  examples = []
  unique_id = 0
  with tf.gfile.GFile(input_file, "r") as reader:
    while True:
      line = tokenization.convert_to_unicode(reader.readline())
      if not line:
        break
      line = line.strip()
      text_a = None
      text_b = None
      m = re.match(r"^(.*) \|\|\| (.*)$", line)
      if m is None:
        text_a = line
      else:
        text_a = m.group(1)
        text_b = m.group(2)
      examples.append(
          InputExample(unique_id=unique_id, text_a=text_a, text_b=text_b))
      unique_id += 1
  return examples

class ff:
    do_lower_case = True
    bert_config_file = '/home/matthewp/data/bert/uncased_L-12_H-768_A-12/bert_config.json'
    vocab_file = '/home/matthewp/data/bert/uncased_L-12_H-768_A-12/vocab.txt'
    init_checkpoint = '/home/matthewp/data/bert/uncased_L-12_H-768_A-12/bert_model.ckpt'
    input_file = 'tt.txt'
    max_seq_length = 128
    batch_size = 4
    use_tpu = False
    master = None
    num_tpu_cores = 8
    use_one_hot_embeddings = False
    do_tokens_only = True


def main(_):
  tf.logging.set_verbosity(tf.logging.INFO)
  print("do_lower_case:", FLAGS.do_lower_case)
  print("do_tokens_only:", FLAGS.do_tokens_only)

  bert_config = modeling.BertConfig.from_json_file(FLAGS.bert_config_file)

  tokenizer = tokenization.FullTokenizer(
      vocab_file=FLAGS.vocab_file, do_lower_case=FLAGS.do_lower_case)

  is_per_host = tf.contrib.tpu.InputPipelineConfig.PER_HOST_V2
  run_config = tf.contrib.tpu.RunConfig(
      master=FLAGS.master,
      tpu_config=tf.contrib.tpu.TPUConfig(
          num_shards=FLAGS.num_tpu_cores,
          per_host_input_for_training=is_per_host))

  examples = read_examples(FLAGS.input_file)

  #if FLAGS.do_tokens_only:
  if True:
      # Get a mapping of unique_id to the orig_to_token_map
      unique_id_to_token_info = {}
      for example in examples:
        original_tokens = example.text_a.strip().split()
        bert_tokens = []
        original_to_bert = []
        tokens_to_remove = []
        bert_tokens.append("[CLS]")
        for orig_token in original_tokens:
          bt = tokenizer.tokenize(orig_token)
          if len(bt) == 0:
            tokens_to_remove.append(orig_token)
          else:
            original_to_bert.append(len(bert_tokens))
            bert_tokens.extend(bt)
        if len(tokens_to_remove) > 0:
            tm = set(tokens_to_remove)
            ot = [token for token in original_tokens if token not in tm]
            original_tokens = ot
        bert_tokens.append("[SEP]")

        assert len(original_to_bert) == len(original_tokens)
        unique_id_to_token_info[example.unique_id] = {
          "original_tokens": original_tokens,
          "original_to_bert": set(original_to_bert),
          "bert_tokens": bert_tokens}

        # the second sentence
        if example.text_b is not None:
            original_tokens = example.text_b.strip().split()
            original_to_bert = []
            tokens_to_remove = []
            for orig_token in original_tokens:
                bt = tokenizer.tokenize(orig_token)
                if len(bt) == 0:
                    tokens_to_remove.append(orig_token)
                else:
                    original_to_bert.append(len(bert_tokens))
                    bert_tokens.extend(bt)
            if len(tokens_to_remove) > 0:
                tm = set(tokens_to_remove)
                ot = [token for token in original_tokens if token not in tm]
                original_tokens = ot
            bert_tokens.append("[SEP]")

            assert len(original_to_bert) == len(original_tokens)
            unique_id_to_token_info[example.unique_id].update({
               "original_tokens2": original_tokens,
               "original_to_bert2": set(original_to_bert),
               "bert_tokens": bert_tokens})

  features = convert_examples_to_features(
      examples=examples, seq_length=FLAGS.max_seq_length, tokenizer=tokenizer)

  if FLAGS.do_tokens_only:
    # check the features!
    for feature in features:
        unique_info = unique_id_to_token_info[feature.unique_id]
        if 'original_tokens2' in unique_info:
            # TODO add a check for the pair case, maybe
            continue
        assert unique_info['bert_tokens'] == feature.tokens
        bert_orig_tokens = [feature.tokens[iii] for iii in sorted(list(unique_info['original_to_bert']))]
        orig_tokens = unique_info['original_tokens']
        assert len(bert_orig_tokens) == len(orig_tokens)
        # first letters of orig_tokens should match bert_orig_tokens
        assert [tok[:len(bt)] for bt, tok in zip(bert_orig_tokens, orig_tokens)] == bert_orig_tokens

  """
    with open('ttt_ids.json', 'w') as fout:
        for k, v in unique_id_to_token_info.items():
            ob = sorted(list(v['original_to_bert']))
            v['original_to_bert'] = ob
        fout.write(json.dumps(unique_id_to_token_info))
  """

  if FLAGS.output_ids_file:
    with open(FLAGS.output_ids_file, 'w') as fout:
        for k, v in unique_id_to_token_info.items():
            ob = sorted(list(v['original_to_bert']))
            v['original_to_bert'] = ob
            if 'original_to_bert2' in v:
                ob2 = sorted(list(v['original_to_bert2']))
                v['original_to_bert2'] = ob2
        fout.write(json.dumps(unique_id_to_token_info))

  if not FLAGS.do_tokens_only:
    unique_id_to_token_info = {}
    for feature in features:
        # don't support pair case with two sentences...
        if len([i for i in feature.input_type_ids if i > 0]) == 0:
            single_sentence = True
        else:
            single_sentence = False

        # this is the total length of sequence, including [CLS], [SEP]
        len_with_cls_sep = len([i for i in feature.input_ids if i > 0])

        if single_sentence:
            unique_id_to_token_info[feature.unique_id] = {
                # remove [CLS], [SEP]
                "original_to_bert": list(range(len_with_cls_sep))[1:-1],
            }
        else:
            # two sentences
            # total length with [SEP] at end for sentence2
            length_sentence2_sep = len([i for i in feature.input_type_ids if i > 0])
            # total length inclyding [CLS] and [SEP] between sentences
            length_sentence1_with_cls_sep = len_with_cls_sep - length_sentence2_sep
            unique_id_to_token_info[feature.unique_id] = {
                # remove [CLS], [SEP]
                "original_to_bert": list(range(length_sentence1_with_cls_sep))[1:-1],
                "original_to_bert2": [iii + length_sentence1_with_cls_sep for iii in range(length_sentence2_sep - 1)]
            }

  model_fn = model_fn_builder(
      bert_config=bert_config,
      init_checkpoint=FLAGS.init_checkpoint,
      use_tpu=FLAGS.use_tpu,
      use_one_hot_embeddings=FLAGS.use_one_hot_embeddings)

  # If TPU is not available, this will fall back to normal Estimator on CPU
  # or GPU.
  estimator = tf.contrib.tpu.TPUEstimator(
      use_tpu=FLAGS.use_tpu,
      model_fn=model_fn,
      config=run_config,
      predict_batch_size=FLAGS.batch_size)

  input_fn = input_fn_builder(
      features=features, seq_length=FLAGS.max_seq_length)

  # Dict of str line# to (num_layers, num_tokens, embedding_size) numpy array
  output_features = {}
  with h5py.File(FLAGS.output_file, "w") as fout:
    for result in tqdm(estimator.predict(input_fn, yield_single_examples=True)):
      unique_id = int(result["unique_id"])
      unique_id_str = str(unique_id)

      # Get the vectors for the sentence
      all_ids = []
      all_features_to_write = []
      ids_to_select = np.array(
        sorted(list(unique_id_to_token_info[unique_id]["original_to_bert"]))
      )
      all_ids.append(ids_to_select)
      if "original_to_bert2" in unique_id_to_token_info[unique_id]:
        ids_to_select = np.array(
            sorted(list(unique_id_to_token_info[unique_id]["original_to_bert2"]))
        )
        all_ids.append(ids_to_select)

      for ids_to_select, key in zip(all_ids, ['original_tokens', 'original_tokens2']):
          n_tokens = len(ids_to_select)
          embed_dim = result["layer_output_0"].shape[1]
          features_to_write = np.empty((NUM_BERT_LAYERS, n_tokens, embed_dim),
                                   dtype=np.float32)
          for layer_num in range(NUM_BERT_LAYERS):
            layer_output = result["layer_output_%d" % layer_num]
            features_to_write[layer_num, :, :] = layer_output[ids_to_select]

          # Check that number of timesteps in features is the same as
          # the number of words.
          if FLAGS.do_tokens_only:
            if len(unique_id_to_token_info[unique_id][key]) != features_to_write.shape[1]:
                raise ValueError("Original tokens: {} with len {}. "
                         "Shape of features_to_write: {}".format(
                           unique_id_to_token_info[unique_id][key],
                           len(unique_id_to_token_info[unique_id][key]),
                           features_to_write.shape))
          all_features_to_write.append(features_to_write)

      if len(all_features_to_write) == 1:
        fout.create_dataset(unique_id_str,
                          all_features_to_write[0].shape, dtype='float32',
                          data=all_features_to_write[0])
      else:
        iid = 2 * unique_id
        fout.create_dataset(str(iid),
                          all_features_to_write[0].shape, dtype='float32',
                          data=all_features_to_write[0])
        fout.create_dataset(str(iid + 1),
                          all_features_to_write[1].shape, dtype='float32',
                          data=all_features_to_write[1])


if __name__ == "__main__":
  flags.mark_flag_as_required("input_file")
  flags.mark_flag_as_required("vocab_file")
  flags.mark_flag_as_required("bert_config_file")
  flags.mark_flag_as_required("init_checkpoint")
  flags.mark_flag_as_required("output_file")
  tf.app.run()
