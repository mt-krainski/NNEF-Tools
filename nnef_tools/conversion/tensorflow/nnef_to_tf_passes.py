# Copyright (c) 2017 The Khronos Group Inc.
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

from __future__ import division, print_function, absolute_import

import math
from collections import namedtuple

from nnef_tools.core import utils
from nnef_tools.io.nnef.nnef_graph import NNEFGraph, NNEFOperation, NNEFTensor


def pre_conversion_pass(g):
    # type: (NNEFGraph)->None
    _transform_extract_padding(g)
    _transform_extract_bias_add(g)
    g.generate_missing_names()
    g.assert_consistent()


# HELPERS

class _InOpPadding(object):
    VALID = "VALID"
    SAME = "SAME"


_InOpAndOutOfOpPadding = namedtuple('_Paddings', ['in_op', 'out_of_op'])  # int[]|None, InOpPadding


def _calculate_padding_elem(upscaled_size, downscaled_size, filter_size, stride, dilation):
    dilated_filter_size = (filter_size - 1) * dilation + 1
    t = (downscaled_size - 1) * stride + dilated_filter_size - upscaled_size
    return math.floor(t / 2), math.ceil(t / 2)


def _calculate_padding(upscaled_shape, downscaled_shape, filter_shape, strides, dilations):
    return [
        _calculate_padding_elem(i, o, f, s, d)
        for i, o, f, s, d in zip(upscaled_shape, downscaled_shape, filter_shape, strides, dilations)
    ]


def _get_paddings(nnefop):
    # type: (NNEFOperation)->_InOpAndOutOfOpPadding
    nnefpadding = nnefop.attribs["padding"]
    nnefborder = nnefop.attribs["border"].lower()

    if len(nnefpadding) == 0 and "pool" in nnefop.name and nnefborder == "ignore":
        return _InOpAndOutOfOpPadding(_InOpPadding.SAME, None)
    elif len(nnefpadding) == 0 and "conv" in nnefop.name and nnefborder == "constant":
        return _InOpAndOutOfOpPadding(_InOpPadding.SAME, None)
    elif not utils.recursive_any(nnefpadding, lambda x: x > 0):
        return _InOpAndOutOfOpPadding(_InOpPadding.VALID, None)
    else:
        if len(nnefpadding) == 0:
            if nnefop.name == "conv":
                input, filter = tuple(nnefop.inputs)[:2]
                output = nnefop.output

                nnefpadding = _calculate_padding(
                    upscaled_shape=input[2:],
                    downscaled_shape=output[2:],
                    filter_shape=filter.shape[2:],
                    strides=nnefop.attribs["stride"],
                    dilations=nnefop.attribs["dilation"]
                )
            elif nnefop.name == "deconv":
                input, filter = tuple(nnefop.inputs)[:2]
                output = nnefop.output

                nnefpadding = _calculate_padding(
                    upscaled_shape=output[2:],
                    downscaled_shape=input[2:],
                    filter_shape=filter.shape[2:],
                    strides=nnefop.attribs["stride"],
                    dilations=nnefop.attribs["dilation"]
                )
            elif nnefop.name in ["argmax_pool", "max_pool_with_index", "max_pool", "avg_pool"]:
                nnefpadding = _calculate_padding(
                    upscaled_shape=nnefop.input.shape,
                    downscaled_shape=nnefop.output.shape,
                    filter_shape=nnefop.attribs["size"],
                    strides=nnefop.attribs["stride"],
                    dilations=nnefop.attribs["dilation"]
                )
            elif nnefop.name == "conv_grad_filter":
                orig_input, output_grad = tuple(nnefop.inputs)[:2]

                nnefpadding = _calculate_padding(
                    upscaled_shape=orig_input.shape[2:],
                    downscaled_shape=output_grad.shape[2:],
                    filter_shape=nnefop.attribs['orig_filter_shape'],
                    strides=nnefop.attribs["stride"],
                    dilations=nnefop.attribs["dilation"]
                )
            elif nnefop.name == "avg_pool_grad":
                output_grad = nnefop.inputs[0]
                nnefpadding = _calculate_padding(
                    upscaled_shape=nnefop.attribs['orig_input_shape'],
                    downscaled_shape=output_grad.shape,
                    filter_shape=nnefop.attribs["size"],
                    strides=nnefop.attribs["stride"],
                    dilations=nnefop.attribs["dilation"])
            elif nnefop.name == "max_pool_grad":
                orig_input, orig_output = nnefop.inputs[:2]
                nnefpadding = _calculate_padding(
                    upscaled_shape=orig_input.shape,
                    downscaled_shape=orig_output.shape,
                    filter_shape=nnefop.attribs["size"],
                    strides=nnefop.attribs["stride"],
                    dilations=nnefop.attribs["dilation"])
            elif nnefop.name == "max_pool_grad_with_index":
                orig_input, orig_index, output_grad = nnefop.inputs[:3]
                nnefpadding = _calculate_padding(
                    upscaled_shape=orig_input.shape,
                    downscaled_shape=output_grad.shape,
                    filter_shape=nnefop.attribs["size"],
                    strides=nnefop.attribs["stride"],
                    dilations=nnefop.attribs["dilation"])
            else:
                assert False

        if utils.recursive_any(nnefpadding, lambda x: x > 0):
            if "conv" in nnefop.name:
                return _InOpAndOutOfOpPadding(_InOpPadding.VALID, [(0, 0), (0, 0)] + nnefpadding)
            else:
                return _InOpAndOutOfOpPadding(_InOpPadding.VALID, nnefpadding)
        else:
            return _InOpAndOutOfOpPadding(_InOpPadding.VALID, None)


# TRANSFORMS

def _transform_extract_padding(g):
    # type:(NNEFGraph)->None
    forward_ops = {"conv", "argmax_pool", "max_pool_with_index", "max_pool", "avg_pool"}
    backward_ops = {"deconv", "conv_grad_filter", "max_pool_grad_with_index", "avg_pool_grad", "max_pool_grad"}
    supported_ops = forward_ops.union(backward_ops)

    for nnefop in list(g.operations):
        if nnefop.name in supported_ops:
            in_op_padding, separate_padding = _get_paddings(nnefop)
            nnefop.attribs["padding"] = in_op_padding

            if nnefop.name in backward_ops:
                assert separate_padding is None

            if separate_padding is not None:
                input = nnefop.inputs[0]
                pad_op = NNEFOperation(
                    graph=g,
                    name="box",
                    inputs=input,
                    attribs=dict(size=[1] * input.rank,
                                 border=nnefop.attribs["border"],
                                 padding=separate_padding),
                    outputs=NNEFTensor(graph=g,
                                       name=None,
                                       shape=[s + p + q for s, (p, q) in zip(input.shape, separate_padding)],
                                       dtype=input.dtype)
                )

                nnefop.inputs = (pad_op.output,) + tuple(nnefop.inputs)[1:]


def _transform_extract_bias_add(g):
    # type:(NNEFGraph)->None

    supported_ops = {"conv", "deconv"}

    for nnefop in list(g.operations):
        if nnefop.name in supported_ops and len(nnefop.inputs) >= 3:
            bias = nnefop.inputs[2]
            nnefop.inputs = tuple(nnefop.inputs)[:2]

            if not (bias.is_constant and bias.data == [0]):
                output_with_bias = nnefop.output
                output_without_bias = NNEFTensor(graph=g,
                                                 name=None,
                                                 dtype=output_with_bias.dtype,
                                                 shape=output_with_bias.shape)
                nnefop.outputs = output_without_bias

                NNEFOperation(graph=g,
                              name="_bias_add",
                              inputs=(output_without_bias, bias),
                              outputs=output_with_bias)
