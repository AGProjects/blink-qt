"""A small convolutional network, exported from ONNX into an npz by scripts/onnx-to-npz.py, run in numpy.

Only what the background blur model (PP-HumanSeg) uses: Conv (no groups, SAME_UPPER or explicit
padding), Relu, Add, bilinear Resize (half_pixel), Concat on channels, Transpose, Softmax. Checked
against onnxruntime: the output differs by less than 1e-6."""

import json
import numpy as np


class NumpyNet(object):
    """Runs a small convolutional network (Conv, Relu, Add, bilinear Resize, Concat, Transpose,
    Softmax) exported from ONNX into an npz: numpy only."""

    def __init__(self, path):
        with np.load(path, allow_pickle=False) as data:
            self.graph = json.loads(str(data['graph']))
            self.weights = {name: data[name] for name in data.files if name != 'graph'}
        # a k x k convolution is k*k matrix products, one per tap of the kernel
        self.taps = {}
        for node in self.graph['nodes']:
            if node['op'] == 'conv':
                assert node['group'] == 1
                w = self.weights[node['w']]
                self.taps[node['w']] = [np.ascontiguousarray(w[:, :, y, x]) for y in range(w.shape[2]) for x in range(w.shape[3])]
                if node['b'] is not None:
                    self.weights[node['b']] = self.weights[node['b']].reshape(-1, 1, 1)
        self._resize_cache = {}

    def __call__(self, x):
        values = {self.graph['input']: x[0].astype(np.float32)}      # batch of one: C x H x W
        for node in self.graph['nodes']:
            op = node['op']
            if op == 'conv':
                y = self._conv(values[node['x']], node)
            elif op == 'relu':
                y = np.maximum(values[node['x']], 0)
            elif op == 'add':
                y = values[node['x']] + values[node['y']]
            elif op == 'resize':
                y = self._resize(values[node['x']], *node['size'])
            elif op == 'concat':
                y = np.concatenate([values[name] for name in node['xs']], axis=0)
            elif op == 'transpose':
                y = values[node['x']][None].transpose(node['perm'])[0]
            elif op == 'softmax':
                v = values[node['x']][None]
                e = np.exp(v - v.max(axis=node['axis'], keepdims=True))
                y = (e / e.sum(axis=node['axis'], keepdims=True))[0]
            values[node['out']] = y
        return values[self.graph['output']][None]

    def _conv(self, x, node):
        w = self.weights[node['w']]
        out_c, in_c, kh, kw = w.shape
        stride, pad = node['stride'], node['pad']
        if node.get('same'):        # SAME_UPPER: out = ceil(in / stride), the odd pixel of padding at the end
            _, h, wd = x.shape
            pads = []
            for size, k in ((h, kh), (wd, kw)):
                total = max((-(-size // stride) - 1) * stride + k - size, 0)
                pads.append((total // 2, total - total // 2))
            if any(sum(p) for p in pads):
                x = np.pad(x, ((0, 0),) + tuple(pads))
        elif pad:
            x = np.pad(x, ((0, 0), (pad, pad), (pad, pad)))
        c, h, wd = x.shape
        oh, ow = (h - kh) // stride + 1, (wd - kw) // stride + 1
        if kh == kw == 1 and stride == 1:
            y = (self.taps[node['w']][0] @ x.reshape(c, -1)).reshape(out_c, oh, ow)
        else:
            y = np.zeros((out_c, oh * ow), dtype=np.float32)
            taps = self.taps[node['w']]
            i = 0
            for dy in range(kh):
                for dx in range(kw):
                    patch = x[:, dy:dy + stride * (oh - 1) + 1:stride, dx:dx + stride * (ow - 1) + 1:stride]
                    y += taps[i] @ patch.reshape(c, -1)
                    i += 1
            y = y.reshape(out_c, oh, ow)
        if node['b'] is not None:
            y += self.weights[node['b']]
        return y

    def _axis(self, size_in, size_out):
        key = (size_in, size_out)
        if key not in self._resize_cache:
            position = (np.arange(size_out, dtype=np.float32) + 0.5) * (size_in / size_out) - 0.5
            position = np.clip(position, 0, size_in - 1)
            low = np.floor(position).astype(np.intp)
            high = np.minimum(low + 1, size_in - 1)
            self._resize_cache[key] = low, high, (position - low).astype(np.float32)
        return self._resize_cache[key]

    def _resize(self, x, height, width):
        c, h, w = x.shape
        if (h, w) == (height, width):
            return x
        low, high, f = self._axis(h, height)
        x = x[:, low, :] * (1 - f)[None, :, None] + x[:, high, :] * f[None, :, None]
        low, high, f = self._axis(w, width)
        return x[:, :, low] * (1 - f) + x[:, :, high] * f
