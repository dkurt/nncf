"""
 Copyright (c) 2022 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""
from itertools import product
from typing import Tuple, List

import onnx
import pytest
import torch
from nncf import NNCFConfig
from nncf.torch.quantization.layers import PTQuantizerSpec
from nncf.torch.quantization.layers import QUANTIZATION_MODULES
from nncf.torch.quantization.layers import QuantizationMode
from nncf.torch.quantization.layers import QuantizerExportMode
from tests.torch.helpers import TwoConvTestModel
from tests.torch.helpers import get_nodes_by_type
from tests.torch.helpers import load_exported_onnx_version
from tests.torch.helpers import register_bn_adaptation_init_args
from tests.torch.helpers import resolve_constant_node_inputs_to_values


def get_config_for_export_mode(should_be_onnx_standard: bool) -> NNCFConfig:
    nncf_config = NNCFConfig()
    nncf_config.update({
        "input_info": {
            "sample_size": [1, 1, 4, 4]
        },
        "compression": {
            "algorithm": "quantization",
            "export_to_onnx_standard_ops": should_be_onnx_standard
        }
    })
    register_bn_adaptation_init_args(nncf_config)
    return nncf_config


def test_onnx_export_to_fake_quantize(tmp_path):
    model = TwoConvTestModel()
    nncf_config = get_config_for_export_mode(should_be_onnx_standard=False)
    onnx_model_proto = load_exported_onnx_version(nncf_config, model,
                                                  path_to_storage_dir=tmp_path)
    num_fq = 0
    num_model_nodes = 0
    num_other_nodes = 0
    # pylint:disable=no-member
    for node in onnx_model_proto.graph.node:
        op_type = node.op_type
        if op_type == 'FakeQuantize':
            num_fq += 1
        elif op_type in ['Conv', 'Constant']:
            num_model_nodes += 1
        else:
            num_other_nodes += 1
    assert num_fq == 4
    assert num_other_nodes == 0


def test_onnx_export_to_quantize_dequantize(tmp_path):
    # It doesn't work with CPU target_device because
    # per-channel quantization is not supported in onnxruntime.
    model = TwoConvTestModel()
    nncf_config = get_config_for_export_mode(should_be_onnx_standard=True)
    nncf_config['target_device'] = 'TRIAL'
    onnx_model_proto = load_exported_onnx_version(nncf_config, model,
                                                  path_to_storage_dir=tmp_path)
    num_q = 0
    num_dq = 0
    num_model_nodes = 0
    num_other_nodes = 0
    # pylint:disable=no-member
    for node in onnx_model_proto.graph.node:
        op_type = node.op_type
        if op_type == 'QuantizeLinear':
            num_q += 1
        elif op_type == 'DequantizeLinear':
            num_dq += 1
        elif op_type in ['Conv', 'Constant']:
            num_model_nodes += 1
        else:
            num_other_nodes += 1
    assert num_q == 4
    assert num_q == num_dq
    assert num_other_nodes == 0


INPUT_TENSOR_SHAPE = (2, 64, 15, 10)
PER_CHANNEL_AQ_SCALE_SHAPE = (1, INPUT_TENSOR_SHAPE[1], 1, 1)


@pytest.mark.parametrize('per_channel, qmode, export_mode',
                         product(
                             [True, False],
                             [QuantizationMode.SYMMETRIC, QuantizationMode.ASYMMETRIC],
                             [QuantizerExportMode.FAKE_QUANTIZE, QuantizerExportMode.ONNX_QUANTIZE_DEQUANTIZE_PAIRS]
                         ))
def test_onnx_export_to_quantize_dequantize_per_channel(per_channel: bool,
                                                        qmode: QuantizationMode,
                                                        export_mode: QuantizerExportMode):
    scale_shape = PER_CHANNEL_AQ_SCALE_SHAPE if per_channel else (1,)
    qspec = PTQuantizerSpec(
        scale_shape=scale_shape,
        num_bits=8,
        mode=qmode,
        signedness_to_force=None,
        logarithm_scale=False,
        narrow_range=False,
        half_range=False,
    )

    q_cls = QUANTIZATION_MODULES.get(qmode)
    quantizer = q_cls(qspec)
    if qmode is QuantizationMode.SYMMETRIC:
        quantizer.scale = torch.nn.Parameter(torch.rand_like(quantizer.scale))
    else:
        quantizer.input_low = torch.nn.Parameter(torch.rand_like(quantizer.input_low))
        quantizer.input_range = torch.nn.Parameter(torch.rand_like(quantizer.input_range))
    # pylint: disable=protected-access
    quantizer._export_mode = export_mode

    x = torch.rand(INPUT_TENSOR_SHAPE)
    if quantizer.per_channel and export_mode is QuantizerExportMode.ONNX_QUANTIZE_DEQUANTIZE_PAIRS:
        with pytest.raises(RuntimeError):
            quantizer.run_export_quantization(x)
    else:
        quantizer.run_export_quantization(x)


class TargetCompressionIdxTestModel(torch.nn.Module):
    CONV2D_TARGET_CHANNEL_COUNT = 5
    CONV2D_TRANSPOSE_TARGET_CHANNEL_COUNT = 10

    def __init__(self):
        super().__init__()
        self.conv = torch.nn.Conv2d(in_channels=1,
                                    out_channels=self.CONV2D_TARGET_CHANNEL_COUNT,
                                    kernel_size=(1, 1))
        self.conv_t = torch.nn.ConvTranspose2d(in_channels=self.CONV2D_TARGET_CHANNEL_COUNT,
                                               out_channels=self.CONV2D_TRANSPOSE_TARGET_CHANNEL_COUNT,
                                               kernel_size=(1, 1))

    def forward(self, x):
        x = self.conv(x)
        x = self.conv_t(x)
        return x


# pylint: disable=no-member

def get_weight_fq_for_conv_node(node: onnx.NodeProto, graph: onnx.GraphProto):
    weight_input_tensor_id = node.input[1]
    matches = [x for x in graph.node if weight_input_tensor_id in x.output]
    assert len(matches) == 1
    match = next(iter(matches))
    assert match.op_type == "FakeQuantize"
    return match


def get_input_low_input_high_for_wfq_node(wfq_node: onnx.NodeProto, graph: onnx.GraphProto) \
        -> Tuple[onnx.AttributeProto, onnx.AttributeProto]:
    assert wfq_node.op_type == "FakeQuantize"
    conv_wfq_inputs = list(resolve_constant_node_inputs_to_values(wfq_node, graph).values())
    return conv_wfq_inputs[1], conv_wfq_inputs[2]


def test_target_compression_idx(tmp_path):
    model = TargetCompressionIdxTestModel()
    nncf_config = get_config_for_export_mode(should_be_onnx_standard=False)
    onnx_model_proto = load_exported_onnx_version(nncf_config, model,
                                                  path_to_storage_dir=tmp_path)
    onnx_graph = onnx_model_proto.graph  # pylint:disable=no-member
    conv_nodes = get_nodes_by_type(onnx_model_proto, "Conv")
    assert len(conv_nodes) == 1
    conv_node = next(iter(conv_nodes))
    conv_wfq_node = get_weight_fq_for_conv_node(conv_node, onnx_graph)
    input_low_attr, input_high_attr = get_input_low_input_high_for_wfq_node(conv_wfq_node,
                                                                            onnx_graph)
    assert input_low_attr.shape == (TargetCompressionIdxTestModel.CONV2D_TARGET_CHANNEL_COUNT, 1, 1, 1)
    assert input_low_attr.shape == input_high_attr.shape

    conv_t_nodes = get_nodes_by_type(onnx_model_proto, "ConvTranspose")
    assert len(conv_t_nodes) == 1
    conv_t_node = next(iter(conv_t_nodes))
    conv_t_wfq_node = get_weight_fq_for_conv_node(conv_t_node, onnx_graph)
    input_low_t_attr, input_high_t_attr = get_input_low_input_high_for_wfq_node(conv_t_wfq_node,
                                                                                onnx_graph)
    assert input_low_t_attr.shape == (1, TargetCompressionIdxTestModel.CONV2D_TRANSPOSE_TARGET_CHANNEL_COUNT, 1, 1)
    assert input_low_t_attr.shape == input_high_t_attr.shape


class ModelWithBranches(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv_1 = torch.nn.Conv2d(2, 2, (1, 1))
        self.conv_2 = torch.nn.Conv2d(2, 2, (1, 1), groups=2)
        self.conv_3 = torch.nn.Conv2d(2, 2, (1, 1), groups=2)


    def forward(self, x):
        x1 = self.conv_1(x)
        x2 = self.conv_2(x)
        x3 = self.conv_3(x)
        x4 = x + x
        return x1, x2, x3, x4

def get_successors(node: onnx.NodeProto, graph: onnx.GraphProto) -> List[onnx.NodeProto]:
    retval = []
    for output_name in node.output:
        for target_node in graph.node:
            if output_name in target_node.input:
                retval.append(target_node)
    return retval

@pytest.mark.parametrize('export_mode', [QuantizerExportMode.FAKE_QUANTIZE,
                                         QuantizerExportMode.ONNX_QUANTIZE_DEQUANTIZE_PAIRS])
def test_branching_fqs_are_not_chained(tmp_path, export_mode):
    nncf_config = NNCFConfig.from_dict({
        "input_info": {
            "sample_size": [1, 2, 2, 2]
        },
        "compression": {
            "algorithm": "quantization",
            "preset": "mixed",
            "ignored_scopes": [
                "/nncf_model_input_0",
                "{re}.*__add__.*"
            ],
            "initializer": {
                "range": {
                    "num_init_samples": 0
                },
                "batchnorm_adaptation": {
                    "num_bn_adaptation_samples": 0
                }
            }
        }
    })
    onnx_model_proto = load_exported_onnx_version(nncf_config, ModelWithBranches(),
                                                  path_to_storage_dir=tmp_path)
    target_node_type = "FakeQuantize" if export_mode is QuantizerExportMode.FAKE_QUANTIZE else "DequantizeLinear"
    quantizer_nodes = get_nodes_by_type(onnx_model_proto, target_node_type)
    # Quantizer nodes should, for this model, immediately be followed by the quantized operation. Chained quantizers
    # mean that the ONNX export was incorrect.
    #pylint:disable=no-member
    follower_node_lists = [get_successors(x, onnx_model_proto.graph) for x in quantizer_nodes]
    follower_nodes = []
    for lst in follower_node_lists:
        follower_nodes += lst
    follower_node_types = [x.op_type for x in follower_nodes]
    assert not any(x == target_node_type for x in follower_node_types)
