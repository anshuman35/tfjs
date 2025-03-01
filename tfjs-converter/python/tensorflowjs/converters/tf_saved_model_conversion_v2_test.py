# Copyright 2018 Google LLC
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
# ==============================================================================
"""Unit tests for artifact conversion to and from Tensorflow SavedModel v2."""

import base64
import glob
import json
import os
import shutil
import tempfile
import unittest
import numpy as np

import tensorflow.compat.v2 as tf
from tensorflow_decision_forests.keras import GradientBoostedTreesModel
from tensorflow.python.eager import def_function
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import tensor_spec
from tensorflow.python.ops import variables
from tensorflow.python.training.tracking import tracking
from tensorflow.python.tools import freeze_graph
from tensorflow.python.saved_model.save import save
import tensorflow_hub as hub
from tensorflowjs import version
from tensorflowjs.converters import graph_rewrite_util
from tensorflowjs.converters import tf_saved_model_conversion_v2
from tensorflowjs.converters.common import ASSETS_DIRECTORY_NAME

SAVED_MODEL_DIR = 'saved_model'
HUB_MODULE_DIR = 'hub_module'
FROZEN_MODEL_DIR = 'frozen_model'

class ConvertTest(tf.test.TestCase):
  def setUp(self):
    super(ConvertTest, self).setUp()
    self._tmp_dir = tempfile.mkdtemp()

  def tearDown(self):
    if os.path.isdir(self._tmp_dir):
      shutil.rmtree(self._tmp_dir)
    super(ConvertTest, self).tearDown()

  def _create_saved_model_v1(self):
    """Create a TensorFlow SavedModel for testing."""

    graph = tf.Graph()
    with graph.as_default():
      x = tf.compat.v1.constant([[37.0, -23.0], [1.0, 4.0]])
      w = tf.compat.v1.get_variable('w', shape=[2, 2])
      y = tf.compat.v1.matmul(x, w)
      output = tf.compat.v1.nn.softmax(y)
      init_op = w.initializer

      # Create a builder.
      save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
      builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(save_dir)

      with tf.compat.v1.Session() as sess:
        # Run the initializer on `w`.
        sess.run(init_op)

        builder.add_meta_graph_and_variables(
            sess, [tf.compat.v1.saved_model.tag_constants.SERVING],
            signature_def_map={
                'serving_default':
                    tf.compat.v1.saved_model \
                        .signature_def_utils.predict_signature_def(
                            inputs={'x': x},
                            outputs={'output': output})
            },
            assets_collection=None)

      builder.save()

  def _create_saved_model_v1_with_hashtable(self):
    """Create a TensorFlow SavedModel V1 with unused hash table for testing."""

    graph = tf.Graph()
    with graph.as_default():
      x = tf.compat.v1.placeholder('int32', [None, 2, 2])
      t = tf.compat.v1.to_float(x)
      w = tf.compat.v1.get_variable('w', shape=[2, 2])
      output = tf.compat.v1.matmul(t, w)
      init_op = w.initializer

      # Add a hash table that is not used by the output.
      keys = tf.constant(['key'])
      values = tf.constant([1])
      initializer = tf.lookup.KeyValueTensorInitializer(keys, values)
      table = tf.lookup.StaticHashTable(initializer, -1)

      # Create a builder.
      save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
      builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(
          save_dir)

      with tf.compat.v1.Session() as sess:
        # Run the initializer on `w`.
        sess.run(init_op)
        table.lookup(keys)
        builder.add_meta_graph_and_variables(
            sess, [tf.compat.v1.saved_model.tag_constants.SERVING],
            signature_def_map={
                'serving_default':
                    tf.compat.v1.saved_model \
                        .signature_def_utils.predict_signature_def(
                            inputs={'t': t},
                            outputs={'output': output})
            },
            assets_collection=None)

      builder.save()

  def _create_saved_model_v2_with_hashtable(self):
    """Create a TensorFlow SavedModel V2 with hash table for testing."""

    class Table(tf.Module):
        def __init__(self):
            super(Table, self).__init__()
            keys = tf.constant(['a', 'b'])
            vals= tf.constant([0, 1])
            init = tf.lookup.KeyValueTensorInitializer(keys, vals)
            self.table = tf.lookup.StaticHashTable(init, -1)

        def initializeTable(self):
            @tf.function
            def lookup(input):
                return self.table.lookup(input)

            return lookup

    model = Table()
    concrete_fn = model.initializeTable().get_concrete_function(
      input=tf.TensorSpec([None], tf.string))

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf.saved_model.save(model, save_dir, signatures={"serving_default": concrete_fn})

  def _create_saved_model_with_fusable_conv2d(self, use_bias):
    """Test a basic model with fusable conv2d."""
    layers = [
        tf.keras.layers.Conv2D(
            16, [3, 3], padding='same', use_bias=use_bias),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.ReLU()
    ]
    model = tf.keras.Sequential(layers)
    model.predict(tf.ones((1, 224, 224, 3)))
    tf.keras.backend.set_learning_phase(0)
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf.saved_model.save(model, save_dir)

  def _create_saved_model_with_fusable_depthwise_conv2d(self):
    """Test a basic model with fusable depthwise conv2d."""
    layers = [
        tf.keras.layers.DepthwiseConv2D(
            1, use_bias=True,
            bias_initializer=tf.initializers.constant(0.25)),
        tf.keras.layers.ReLU()
    ]
    model = tf.keras.Sequential(layers)
    model.predict(tf.ones((1, 2, 2, 3)))
    tf.keras.backend.set_learning_phase(0)
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf.saved_model.save(model, save_dir)

  def _create_saved_model_with_fusable_addV2(self):
    """Test a basic model with fusable addV2."""
    @tf.function
    def conv2d_addV2_depthwise_addV2(x):
      filter = tf.ones([1, 1, 1, 1])
      bias = tf.constant([100], dtype=dtypes.float32)
      res = tf.raw_ops.Conv2D(
        input=x, filter=filter, strides=[1, 1, 1, 1], padding="VALID")
      res = tf.raw_ops.AddV2(x=res, y=bias)
      res = tf.raw_ops.DepthwiseConv2dNative(
        input=res, filter=filter, strides=[1, 1, 1, 1], padding="VALID")
      res = tf.raw_ops.AddV2(x=res, y=bias)
      return res
    root = tracking.AutoTrackable()
    root.f = conv2d_addV2_depthwise_addV2
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([1, 1, 1, 1], dtypes.float32))
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_unfusable_addV2(self):
    """Test a basic model with fusable addV2."""
    @tf.function
    def addV2_conv2d(x):
      bias = tf.constant([100], dtype=dtypes.float32)
      filter = tf.ones([1, 1, 1, 1])
      res = tf.raw_ops.AddV2(x=x, y=bias)
      res = tf.raw_ops.Conv2D(
        input=res, filter=filter, strides=[1, 1, 1, 1], padding="VALID")
      return res
    root = tracking.AutoTrackable()
    root.f = addV2_conv2d
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([1, 1, 1, 1], dtypes.float32))
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_prelu(self):
    """Test a basic model with fusable conv2d."""
    layers = [
        tf.keras.layers.Conv2D(
            16, [3, 3], padding='same', use_bias=True,
            bias_initializer=tf.initializers.constant(0.25)),
        tf.keras.layers.PReLU(alpha_initializer=tf.initializers.constant(0.25)),
        tf.keras.layers.DepthwiseConv2D(
            1, use_bias=True,
            bias_initializer=tf.initializers.constant(0.25)),
        tf.keras.layers.PReLU(alpha_initializer=tf.initializers.constant(0.25))
    ]
    model = tf.keras.Sequential(layers)
    model.predict(tf.ones((1, 224, 224, 3)))
    tf.keras.backend.set_learning_phase(0)
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf.saved_model.save(model, save_dir)

  def _create_saved_model_with_unfusable_prelu(self):
    """Test a basic model with unfusable prelu."""
    layers = [
        tf.keras.layers.ReLU(),
        tf.keras.layers.PReLU(alpha_initializer=tf.initializers.constant(0.25))
    ]
    model = tf.keras.Sequential(layers)
    model.predict(tf.ones((1, 224, 3)))
    tf.keras.backend.set_learning_phase(0)
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf.saved_model.save(model, save_dir)

  def _create_saved_model(self):
    """Test a basic model with functions to make sure functions are inlined."""
    input_data = constant_op.constant(1., shape=[1])
    root = tracking.AutoTrackable()
    root.v1 = variables.Variable(3.)
    root.v2 = variables.Variable(2.)
    root.f = def_function.function(lambda x: root.v1 * root.v2 * x)
    to_save = root.f.get_concrete_function(input_data)

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_fusable_matmul(self):
    """Test a fusable matmul model."""
    input_data = constant_op.constant(1., shape=[1, 1])
    bias_data = constant_op.constant(1., shape=[1])
    root = tracking.AutoTrackable()
    root.v2 = variables.Variable([[2.]])
    root.f = def_function.function(
        lambda x: tf.nn.relu(tf.nn.bias_add(tf.matmul(x, root.v2),
                                            bias_data)))
    to_save = root.f.get_concrete_function(input_data)

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_control_flow(self):
    """Test a basic model with control flow to inlined."""
    @tf.function
    def find_next_odd(v):
      v1 = v + 1
      while tf.equal(v1 % 2, 0):
        v1 = v1 + 1
      return v1
    root = tracking.AutoTrackable()
    root.f = find_next_odd
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([], dtypes.int32))

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_tfdf(self):
    """Test a basic TFDF model."""
    P = 5
    NUM_EXAMPLES = 10
    NUM_FEATURES = 4

    x_train = np.random.uniform(size=(NUM_EXAMPLES, NUM_FEATURES))
    y_train = np.random.uniform(size=NUM_EXAMPLES) > 0.5
    w_train = y_train * (P - 1) + 1  # 1 or p depending on the class.

    model = GradientBoostedTreesModel()
    model.fit(x=x_train, y=y_train, sample_weight=w_train)

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    model.save(save_dir)

  def _create_unsupported_saved_model(self):
    root = tracking.AutoTrackable()
    root.w = variables.Variable(tf.random.uniform([2, 2]))

    @def_function.function
    def exported_function(x):
      root.x = constant_op.constant([[37.0, -23.0], [1.0, 4.0]])
      root.y = tf.matmul(root.x, root.w)
      # unsupported op: linalg.diag
      root.z = tf.linalg.diag(root.y)
      return root.z * x

    root.f = exported_function
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([], dtypes.float32))

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_debug_ops(self):
    root = tracking.AutoTrackable()
    root.w = variables.Variable(tf.random.uniform([2, 2]))

    @def_function.function
    def exported_function(x):
      root.x = constant_op.constant([[37.0, -23.0], [1.0, 4.0]])
      root.y = tf.matmul(root.x, root.w)
      tf.compat.v1.Print(root.x, [root.x])
      tf.compat.v1.Assert(tf.greater(tf.reduce_max(root.x), 0), [root.x])
      tf.compat.v1.check_numerics(root.x, 'NaN found')
      return root.y * x

    root.f = exported_function
    to_save = root.f.get_concrete_function(
        tensor_spec.TensorSpec([], dtypes.float32))

    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    save(root, save_dir, to_save)

  def _create_saved_model_with_structured_outputs(self):
    def create_input(name):
      return tf.keras.layers.Input(name=name, shape=(1,), dtype=tf.float32)

    input1 = create_input("input1")
    input3 = create_input("input3")
    input2 = create_input("input2")

    output1 = tf.keras.layers.Dense(1, name='a')
    output1 = output1(tf.keras.layers.concatenate([input1, input3], axis=1))
    output2 = tf.keras.layers.Dense(1, name='b')(input2)
    output3 = tf.keras.layers.Multiply(name='c')([output1, output2])

    inputs = {
        "input1": input1,
        "input3": input3,
        "input2": input2
    }

    outputs = {
        "a": output1,
        "c": output3,
        "b": output2
    }

    model = tf.keras.Model(inputs=inputs, outputs=outputs)
    save_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf.saved_model.save(model, save_dir)

  def _create_hub_module(self):
    # Module function that doubles its input.
    def double_module_fn():
      w = tf.Variable([2.0, 4.0])
      x = tf.compat.v1.placeholder(dtype=tf.float32)
      hub.add_signature(inputs=x, outputs=x*w)
    graph = tf.Graph()
    with graph.as_default():
      spec = hub.create_module_spec(double_module_fn)
      m = hub.Module(spec)
    # Export the module.
    with tf.compat.v1.Session(graph=graph) as sess:
      sess.run(tf.compat.v1.global_variables_initializer())
      m.export(os.path.join(self._tmp_dir, HUB_MODULE_DIR), sess)

  def create_frozen_model(self):
    graph = tf.Graph()
    saved_model_dir = os.path.join(self._tmp_dir, FROZEN_MODEL_DIR)
    with graph.as_default():
      x = tf.constant([[37.0, -23.0], [1.0, 4.0]])
      w = tf.Variable(tf.random.uniform([2, 2]))
      y = tf.matmul(x, w)
      tf.nn.softmax(y)
      init_op = w.initializer

      # Create a builder
      builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(
          saved_model_dir)

      with tf.compat.v1.Session() as sess:
        # Run the initializer on `w`.
        sess.run(init_op)

        builder.add_meta_graph_and_variables(
            sess, [tf.compat.v1.saved_model.tag_constants.SERVING],
            signature_def_map=None,
            assets_collection=None)

      builder.save()

    frozen_file = os.path.join(self._tmp_dir, FROZEN_MODEL_DIR, 'model.frozen')
    freeze_graph.freeze_graph(
        '',
        '',
        True,
        '',
        "Softmax",
        '',
        '',
        frozen_file,
        True,
        '',
        saved_model_tags=tf.compat.v1.saved_model.tag_constants.SERVING,
        input_saved_model_dir=saved_model_dir)

  def test_convert_saved_model_v1(self):
    self._create_saved_model_v1()

    input_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    output_dir = os.path.join(input_dir, 'js')
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        input_dir,
        output_dir
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'js')
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)
    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(glob.glob(os.path.join(output_dir, 'group*-*')))

  def test_convert_saved_model_v1_with_hashtable(self):
    self._create_saved_model_v1_with_hashtable()

    input_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    output_dir = os.path.join(input_dir, 'js')
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        input_dir,
        output_dir
    )

    expected_weights_manifest = [{
        'paths': ['group1-shard1of1.bin'],
        'weights': [
            {'dtype': 'float32', 'name': 'w', 'shape': [2, 2]},
            {'dtype': 'string', 'name': 'Const', 'shape': [1]},
            {'dtype': 'int32', 'name': 'Const_1', 'shape': [1]}
        ]}]

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'js')
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)
    self.assertTrue(model_json['modelInitializer'])

    for node in model_json['modelTopology']['node']:
      if node['name'] == 'ToFloat' and node['op'] == 'Placeholder':
        self.assertEqual(node['attr']['shape'],
                         {'shape': {'dim': [
                             {'size': '-1'}, {'size': '2'}, {'size': '2'}]}})

    weights_manifest = model_json['weightsManifest']
    self.assertEqual(weights_manifest, expected_weights_manifest)
    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(glob.glob(os.path.join(output_dir, 'group*-*')))

  def test_convert_saved_model_v2_with_hashtable(self):
    self._create_saved_model_v2_with_hashtable()

    input_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    output_dir = os.path.join(input_dir, 'js')
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        input_dir,
        output_dir
    )

    expected_signature = {
      'inputs': {
        'input': {
          'name': 'input:0',
          'dtype': 'DT_STRING',
          'tensorShape': {'dim': [{'size': '-1'}]}
        },
        'unknown:0': {
          'name': 'unknown:0',
          'dtype': 'DT_RESOURCE',
          'tensorShape': {},
          'resourceId': None
        }
    },
      'outputs': {
        'output_0': {
          'name': 'Identity:0',
          'dtype': 'DT_INT32',
          'tensorShape': {'dim': [{'size': '-1'}]}
        }
      }
    }

    expected_initializer_signature = {
      'outputs': {
        'Identity:0': {
          'name': 'Identity:0',
          'dtype': 'DT_RESOURCE',
          'tensorShape': {},
          'resourceId': None
        }
      }
    }

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'js')
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)

    # Check resource ids match which indicates the initializer output is mapped
    # to the inference input.
    signature_resource_id = model_json['signature']['inputs']['unknown:0']['resourceId']
    initializer_resource_id = model_json['initializerSignature']['outputs']['Identity:0']['resourceId']
    self.assertTrue(signature_resource_id)
    self.assertEqual(signature_resource_id, initializer_resource_id)

    # Update expected signatures with resourceId since it is a runtime value.
    expected_signature['inputs']['unknown:0']['resourceId'] = signature_resource_id
    expected_initializer_signature['outputs']['Identity:0']['resourceId'] = signature_resource_id
    self.assertEqual(model_json['signature'], expected_signature)
    self.assertEqual(model_json['initializerSignature'], expected_initializer_signature)

    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    model_ops = [node['op'] for node in model_json['modelTopology']['node']]
    self.assertIn('LookupTableFindV2', model_ops)

    self.assertTrue(model_json['modelInitializer'])
    initializer_ops = [node['op'] for node in model_json['modelInitializer']['node']]
    self.assertIn('HashTableV2', initializer_ops)
    self.assertIn('LookupTableImportV2', initializer_ops)

    weights_manifest = model_json['weightsManifest'][0]
    self.assertEqual(weights_manifest['paths'], ['group1-shard1of1.bin'])
    self.assertEqual(weights_manifest['weights'][0],
                     {'name': 'unknown_0', 'shape': [], 'dtype': 'int32'})
    # Only check weights and dtype since name may vary between TF versions.
    self.assertEqual(weights_manifest['weights'][1]['shape'], [2])
    self.assertEqual(weights_manifest['weights'][1]['dtype'], 'string')
    self.assertEqual(weights_manifest['weights'][2]['shape'], [2])
    self.assertEqual(weights_manifest['weights'][2]['dtype'], 'int32')

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(glob.glob(os.path.join(output_dir, 'group*-*')))

  def test_convert_saved_model_v1_with_metadata(self):
    self._create_saved_model_v1()

    input_dir = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    output_dir = os.path.join(input_dir, 'js')

    metadata_json = {'a': 1}
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        input_dir,
        output_dir,
        metadata={'key': metadata_json}
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'js')
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertEqual(metadata_json, model_json['userDefinedMetadata']['key'])

  def test_convert_saved_model(self):
    self._create_saved_model()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)
    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])

  def test_convert_saved_model_with_frozen_file(self):
    self._create_saved_model()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        frozen_graph_dir=os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    frozen_file_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR,
                                    'model.json.frozen')
    # Check model.json.frozen exist.
    self.assertTrue(
        glob.glob(frozen_file_path))

  def test_convert_saved_model_with_metadata(self):
    self._create_saved_model()

    metadata_json = {'a': 1}

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        metadata={'key': metadata_json}
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertEqual(metadata_json, model_json['userDefinedMetadata']['key'])

  def test_convert_saved_model_with_fused_conv2d(self):
    for use_bias in [True, False]:
      self._create_saved_model_with_fusable_conv2d(use_bias)
      tf_saved_model_conversion_v2.convert_tf_saved_model(
          os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
          os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
      )

      tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
      # Check model.json and weights manifest.
      with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
        model_json = json.load(f)
      self.assertTrue(model_json['modelTopology'])
      self.assertIsNot(model_json['modelTopology']['versions'], None)
      signature = model_json['signature']
      self.assertIsNot(signature, None)
      self.assertIsNot(signature['inputs'], None)
      self.assertIsNot(signature['outputs'], None)

      nodes = model_json['modelTopology']['node']

      fused_op = None
      for node in nodes:
        self.assertNotIn('BatchNorm', node['op'])
        self.assertNotIn('Relu', node['op'])
        self.assertNotIn('BiasAdd', node['op'])
        if node['op'] == '_FusedConv2D':
          fused_op = node
      self.assertIsNot(fused_op, None)
      self.assertEqual(
          base64.b64decode(fused_op['attr']['fused_ops']['list']['s'][0]),
          b'BiasAdd')
      self.assertEqual(
          base64.b64decode(fused_op['attr']['fused_ops']['list']['s'][1]),
          b'Relu')

      # Check meta-data in the artifact JSON.
      self.assertEqual(model_json['format'], 'graph-model')
      self.assertEqual(
          model_json['convertedBy'],
          'TensorFlow.js Converter v%s' % version.version)
      self.assertEqual(model_json['generatedBy'],
                       tf.__version__)
      self.assertTrue(
          glob.glob(
              os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_fused_matmul(self):
    self._create_saved_model_with_fusable_matmul()
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    nodes = model_json['modelTopology']['node']
    fused_op = None
    for node in nodes:
      self.assertNotEqual(node['op'], 'MatMul')
      self.assertNotIn('Relu', node['op'])
      self.assertNotIn('BiasAdd', node['op'])
      if node['op'] == graph_rewrite_util.FUSED_MATMUL:
        fused_op = node
    self.assertIsNot(fused_op, None)
    self.assertIsNot(fused_op['attr']['transpose_a'], None)
    self.assertIsNot(fused_op['attr']['transpose_b'], None)
    self.assertEqual(
        base64.b64decode(fused_op['attr']['fused_ops']['list']['s'][0]),
        b'BiasAdd')
    self.assertEqual(
        base64.b64decode(fused_op['attr']['fused_ops']['list']['s'][1]),
        b'Relu')

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_fused_depthwise_conv2d(self):
    self._create_saved_model_with_fusable_depthwise_conv2d()
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    nodes = model_json['modelTopology']['node']

    fused_op = None
    for node in nodes:
      self.assertNotIn('BatchNorm', node['op'])
      self.assertNotIn('Relu', node['op'])
      self.assertNotIn('BiasAdd', node['op'])
      if node['op'] == graph_rewrite_util.FUSED_DEPTHWISE_CONV2D:
        fused_op = node
    self.assertIsNot(fused_op, None)
    self.assertIsNot(fused_op['attr']['dilations'], None)
    self.assertIsNot(fused_op['attr']['strides'], None)
    self.assertEqual(
        base64.b64decode(fused_op['attr']['fused_ops']['list']['s'][0]),
        b'BiasAdd')
    self.assertEqual(
        base64.b64decode(fused_op['attr']['fused_ops']['list']['s'][1]),
        b'Relu')

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_unfusable_addV2(self):
    self._create_saved_model_with_unfusable_addV2()
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    nodes = model_json['modelTopology']['node']

    # check if AddV2 op exists
    addV2_op = None
    for node in nodes:
      if node['op'] == 'AddV2':
        addV2_op = node
        break
    self.assertTrue(addV2_op)

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_fusable_addV2(self):
    self._create_saved_model_with_fusable_addV2()
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    nodes = model_json['modelTopology']['node']

    # Check if AddV2 is fused to Conv2D and Depthwise ops.
    fused_conv2d_op = None
    fused_depthwise_op = None
    for node in nodes:
      self.assertNotEqual('Conv2D', node['op'])
      self.assertNotEqual('DepthwiseConv2dNative', node['op'])
      self.assertNotEqual('AddV2', node['op'])
      self.assertNotEqual('BiasAdd', node['op'])
      if node['op'] == graph_rewrite_util.FUSED_CONV2D:
        fused_conv2d_op = node
      elif node['op'] == graph_rewrite_util.FUSED_DEPTHWISE_CONV2D:
        fused_depthwise_op = node
    self.assertIsNot(fused_conv2d_op, None)
    self.assertIsNot(fused_depthwise_op, None)
    fused_conv2d_ops = list(map(base64.b64decode,
                         fused_conv2d_op['attr']['fused_ops']['list']['s']))
    self.assertEqual(fused_conv2d_ops, [b'BiasAdd'])
    self.assertEqual(fused_conv2d_op['attr']['num_args']['i'], '1')
    fused_depthwise_ops = list(
        map(base64.b64decode,
            fused_depthwise_op['attr']['fused_ops']['list']['s']))
    self.assertEqual(fused_depthwise_ops, [b'BiasAdd'])
    self.assertEqual(fused_depthwise_op['attr']['num_args']['i'], '1')

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_prelu(self):
    self._create_saved_model_with_prelu()
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    nodes = model_json['modelTopology']['node']

    prelu_op = None
    fused_op = None
    depthwise_fused_op = None
    for node in nodes:
      if node['op'] == 'Prelu':
        prelu_op = node
      if node['op'] == graph_rewrite_util.FUSED_CONV2D:
        fused_op = node
      if node['op'] == graph_rewrite_util.FUSED_DEPTHWISE_CONV2D:
        depthwise_fused_op = node
    self.assertTrue(prelu_op is None)
    self.assertIsNot(fused_op, None)
    self.assertIsNot(depthwise_fused_op, None)

    fused_ops = list(map(base64.b64decode,
                         fused_op['attr']['fused_ops']['list']['s']))
    self.assertEqual(fused_ops, [b'BiasAdd', b'Prelu'])
    self.assertEqual(fused_op['attr']['num_args']['i'], '2')
    depthwise_fused_ops = list(
        map(base64.b64decode,
            depthwise_fused_op['attr']['fused_ops']['list']['s']))
    self.assertEqual(depthwise_fused_ops, [b'BiasAdd', b'Prelu'])
    self.assertEqual(depthwise_fused_op['attr']['num_args']['i'], '2')
    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_unfusable_prelu(self):
    self._create_saved_model_with_unfusable_prelu()
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    nodes = model_json['modelTopology']['node']

    prelu_op = None
    for node in nodes:
      if node['op'] == 'Prelu':
        prelu_op = node
        break

    self.assertTrue(prelu_op)

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_control_flow(self):
    self._create_saved_model_with_control_flow()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])

    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_control_flow_v2(self):
    self._create_saved_model_with_control_flow()

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        tfjs_path, tfjs_path, control_flow_v2=True
    )

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])

    add_y_weight = None
    for weight in weights_manifest[0]['weights']:
      if 'add/y' in weight['name']:
        add_y_weight = weight

    self.assertIsNot(add_y_weight, None)
    self.assertFalse(add_y_weight['name'].startswith('add/y'))

    nodes = model_json['modelTopology']['node']

    while_op = None
    for node in nodes:
      self.assertNotIn('Merge', node['op'])
      self.assertNotIn('Switch', node['op'])
      if node['op'] == 'StatelessWhile':
        while_op = node
    self.assertIsNot(while_op, None)
    # Check meta-data in the artifact JSON.
    self.assertEqual(model_json['format'], 'graph-model')
    self.assertEqual(
        model_json['convertedBy'],
        'TensorFlow.js Converter v%s' % version.version)
    self.assertEqual(model_json['generatedBy'],
                     tf.__version__)
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_with_tfdf(self):
    self._create_saved_model_with_tfdf()

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        tfjs_path, tfjs_path, skip_op_check=True
    )

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)

    # Check TFDF ops are present.
    model_ops = [node['op'] for node in model_json['modelTopology']['node']]
    self.assertIn('SimpleMLInferenceOpWithHandle', model_ops)

    initializer_ops = [node['op'] for node in model_json['modelInitializer']['node']]
    self.assertIn('SimpleMLCreateModelResource', initializer_ops)
    self.assertIn('SimpleMLLoadModelFromPathWithHandle', initializer_ops)

    # Check assets containing TFDF files were copied over.
    self.assertTrue(
        os.path.exists(
            os.path.join(tfjs_path, ASSETS_DIRECTORY_NAME + '.zip')))

  def test_convert_saved_model_sharded(self):
    self._create_saved_model()
    model_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    # Do initial conversion without sharding.
    tf_saved_model_conversion_v2.convert_tf_saved_model(model_path, tfjs_path)
    weight_files = glob.glob(os.path.join(tfjs_path, 'group*.bin'))

    # Get size of weights in bytes after graph optimizations.
    optimized_total_weight = sum([os.path.getsize(f) for f in weight_files])

    # Due to the shard size, there ought to be 2 shards after conversion.
    weight_shard_size_bytes = int(optimized_total_weight * 0.8)

    tfjs_path = os.path.join(self._tmp_dir, 'sharded_model')
    # Convert Saved Model again with shard argument set.
    tf_saved_model_conversion_v2.convert_tf_saved_model(
        model_path, tfjs_path,
        weight_shard_size_bytes=weight_shard_size_bytes)

    weight_files = sorted(glob.glob(os.path.join(tfjs_path, 'group*.bin')))
    self.assertEqual(len(weight_files), 2)
    weight_file_sizes = [os.path.getsize(f) for f in weight_files]

    self.assertEqual(sum(weight_file_sizes), optimized_total_weight)
    self.assertLess(weight_file_sizes[1], weight_file_sizes[0])

  def test_optimizer_add_unsupported_op(self):
    self._create_unsupported_saved_model()
    with self.assertRaisesRegexp(  # pylint: disable=deprecated-method
        ValueError, r'^Unsupported Ops'):
      tf_saved_model_conversion_v2.convert_tf_saved_model(
          os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
          os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
      )

  def test_convert_saved_model_skip_op_check(self):
    self._create_unsupported_saved_model()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR), skip_op_check=True
    )

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  # (TODO: piyu) disable this test, need to change
  # convert_variables_to_constants_v2 to set function_optimization=aggressive.
  @unittest.skip('not supported')
  def test_convert_saved_model_strip_debug_ops(self):
    self._create_saved_model_with_debug_ops()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        strip_debug_ops=True)

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_saved_model_structured_outputs_true(self):
    self._create_saved_model_with_structured_outputs()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        use_structured_outputs_names=True)

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    self.assertEqual(["a", "b", "c"],
                     model_json['userDefinedMetadata']['structuredOutputKeys'])

  def test_convert_saved_model_structured_outputs_false(self):
    self._create_saved_model_with_structured_outputs()

    tf_saved_model_conversion_v2.convert_tf_saved_model(
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR),
        os.path.join(self._tmp_dir, SAVED_MODEL_DIR))

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertIs(model_json.get('userDefinedMetadata'), None)

  def test_convert_hub_module_v1(self):
    self._create_hub_module()
    module_path = os.path.join(self._tmp_dir, HUB_MODULE_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    tf_saved_model_conversion_v2.convert_tf_hub_module(module_path, tfjs_path)

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])

    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_hub_module_v1_sharded(self):
    self._create_hub_module()
    module_path = os.path.join(self._tmp_dir, HUB_MODULE_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    # Do initial conversion without sharding.
    tf_saved_model_conversion_v2.convert_tf_hub_module(module_path, tfjs_path)
    weight_files = glob.glob(os.path.join(tfjs_path, 'group*.bin'))

    # Get size of weights in bytes after graph optimizations.
    optimized_total_weight = sum([os.path.getsize(f) for f in weight_files])

    # Due to the shard size, there ought to be 3 shards after conversion.
    weight_shard_size_bytes = int(optimized_total_weight * 0.4)

    tfjs_path = os.path.join(self._tmp_dir, 'sharded_model')
    # Convert Hub model again with shard argument set.
    tf_saved_model_conversion_v2.convert_tf_hub_module(
        module_path, tfjs_path,
        weight_shard_size_bytes=weight_shard_size_bytes)

    weight_files = sorted(glob.glob(os.path.join(tfjs_path, 'group*.bin')))
    self.assertEqual(len(weight_files), 3)
    weight_file_sizes = [os.path.getsize(f) for f in weight_files]

    self.assertEqual(sum(weight_file_sizes), optimized_total_weight)
    self.assertEqual(weight_file_sizes[0], weight_file_sizes[1])
    self.assertLess(weight_file_sizes[2], weight_file_sizes[0])

  def test_convert_hub_module_v1_with_metadata(self):
    self._create_hub_module()
    module_path = os.path.join(self._tmp_dir, HUB_MODULE_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    metadata_json = {'a': 1}
    tf_saved_model_conversion_v2.convert_tf_hub_module(
        module_path, tfjs_path, metadata={'key': metadata_json})

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertEqual(metadata_json, model_json['userDefinedMetadata']['key'])

  def test_convert_hub_module_v2(self):
    self._create_saved_model()
    module_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    tf_saved_model_conversion_v2.convert_tf_hub_module(
        module_path, tfjs_path, "serving_default", "serve")

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])

    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

  def test_convert_hub_module_v2_with_metadata(self):
    self._create_saved_model()
    module_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)

    metadata_json = {'a': 1}
    tf_saved_model_conversion_v2.convert_tf_hub_module(
        module_path, tfjs_path, "serving_default", "serve",
        metadata={'key': metadata_json})

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertEqual(metadata_json, model_json['userDefinedMetadata']['key'])

  def test_convert_frozen_model(self):
    self.create_frozen_model()
    print(glob.glob(
        os.path.join(self._tmp_dir, FROZEN_MODEL_DIR, '*')))

    tf_saved_model_conversion_v2.convert_tf_frozen_model(
        os.path.join(self._tmp_dir, FROZEN_MODEL_DIR, 'model.frozen'),
        'Softmax',
        os.path.join(self._tmp_dir, FROZEN_MODEL_DIR))

    tfjs_path = os.path.join(self._tmp_dir, FROZEN_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    # frozen model signature has no input nodes.
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, FROZEN_MODEL_DIR, 'group*-*')))

  def test_convert_frozen_model_with_metadata(self):
    self.create_frozen_model()
    print(glob.glob(
        os.path.join(self._tmp_dir, FROZEN_MODEL_DIR, '*')))

    metadata_json = {'a': 1}
    tf_saved_model_conversion_v2.convert_tf_frozen_model(
        os.path.join(self._tmp_dir, FROZEN_MODEL_DIR, 'model.frozen'),
        'Softmax',
        os.path.join(self._tmp_dir, FROZEN_MODEL_DIR),
        metadata={'key': metadata_json})

    tfjs_path = os.path.join(self._tmp_dir, FROZEN_MODEL_DIR)
    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertEqual(metadata_json, model_json['userDefinedMetadata']['key'])

  def test_convert_keras_model_to_saved_model(self):
    keras_model = tf.keras.Sequential(
        [tf.keras.layers.Dense(1, input_shape=[2])])

    tfjs_path = os.path.join(self._tmp_dir, SAVED_MODEL_DIR)
    tf_saved_model_conversion_v2.convert_keras_model_to_graph_model(
        keras_model, tfjs_path)

    # Check model.json and weights manifest.
    with open(os.path.join(tfjs_path, 'model.json'), 'rt') as f:
      model_json = json.load(f)
    self.assertTrue(model_json['modelTopology'])
    self.assertIsNot(model_json['modelTopology']['versions'], None)
    signature = model_json['signature']
    self.assertIsNot(signature, None)
    self.assertIsNot(signature['inputs'], None)
    self.assertIsNot(signature['outputs'], None)

    weights_manifest = model_json['weightsManifest']
    self.assertCountEqual(weights_manifest[0]['paths'],
                          ['group1-shard1of1.bin'])
    self.assertIn('weights', weights_manifest[0])
    self.assertTrue(
        glob.glob(
            os.path.join(self._tmp_dir, SAVED_MODEL_DIR, 'group*-*')))

if __name__ == '__main__':
  tf.test.main()
