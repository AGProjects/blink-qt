#!/usr/bin/env python3

"""Convert an ONNX model into the npz blink.webrtc.numpy_net runs (weights, and the graph as JSON).

Usage: python3 scripts/onnx-to-npz.py model.onnx resources/models/name.npz  (needs the onnx package)"""

import json, sys, numpy as np, onnx
from onnx import helper, numpy_helper
m = onnx.load(sys.argv[1]); g = m.graph
tensors = {i.name: numpy_helper.to_array(i) for i in g.initializer}
names = {}
def key(n):
    if n not in names: names[n] = 't%d' % len(names)
    return names[n]
nodes = []
for n in g.node:
    a = {x.name: helper.get_attribute_value(x) for x in n.attribute}
    if n.op_type == 'Conv':
        node = dict(op='conv', x=key(n.input[0]), w=key(n.input[1]), b=key(n.input[2]) if len(n.input) > 2 else None,
                    stride=a.get('strides', [1, 1])[0], pad=a.get('pads', [0]*4)[0], group=a.get('group', 1),
                    same=a.get('auto_pad', b'NOTSET') == b'SAME_UPPER')
        assert a.get('dilations', [1, 1]) == [1, 1] and len(set(a.get('pads', [0]*4))) == 1
        assert a.get('auto_pad', b'NOTSET') in (b'NOTSET', b'SAME_UPPER')
    elif n.op_type == 'Relu':
        node = dict(op='relu', x=key(n.input[0]))
    elif n.op_type == 'Add':
        node = dict(op='add', x=key(n.input[0]), y=key(n.input[1]))
    elif n.op_type == 'Resize':
        assert a['mode'] == b'linear' and a['coordinate_transformation_mode'] == b'half_pixel'
        size = tensors[n.input[3]].tolist()
        node = dict(op='resize', x=key(n.input[0]), size=[int(v) for v in size[2:]])
    elif n.op_type == 'Concat':
        assert a['axis'] == 1
        node = dict(op='concat', xs=[key(i) for i in n.input])
    elif n.op_type == 'Transpose':
        node = dict(op='transpose', x=key(n.input[0]), perm=list(a['perm']))
    elif n.op_type == 'Softmax':
        node = dict(op='softmax', x=key(n.input[0]), axis=a['axis'])
    else:
        raise SystemExit(n.op_type)
    node['out'] = key(n.output[0])
    nodes.append(node)
arrays = {}
for n in nodes:
    for k in ('w', 'b'):
        if n.get(k) is not None:
            src = [s for s, d in names.items() if d == n[k]][0]
            arrays[n[k]] = tensors[src].astype(np.float32)
graph = dict(input=key(g.input[0].name), output=key(g.output[0].name), nodes=nodes)
np.savez_compressed(sys.argv[2], graph=np.array(json.dumps(graph)), **arrays)
