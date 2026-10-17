"""Tests for blink.screen_pointer. Run from the top of the tree: python3 -m unittest tests.test_screen_pointer"""

import importlib.util
import json
import os
import sys
import types
import unittest


def _load(name, filename):
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.screen_pointer')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
SP = _load('blink.screen_pointer', 'screen_pointer.py')


def tearDownModule():
    for name, module in _saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class WireFormatTests(unittest.TestCase):
    """What sylk-mobile sends and expects."""

    def test_capabilities(self):
        mobile = json.dumps({'version': 1, 'capabilities': ['screen-sharing', 'screen-request', 'pointer', 'conference-request', 7]})
        self.assertEqual(SP.parse_capabilities(mobile), ['screen-sharing', 'screen-request', 'pointer', 'conference-request'])
        self.assertIsNone(SP.parse_capabilities('not json'))
        self.assertEqual(json.loads(SP.build_capabilities(SP.my_capabilities(can_share_screen=False))),
                         {'version': 1, 'capabilities': ['pointer']})

    def test_request(self):
        request_id, body = SP.build_request(now=1700000000.0)
        data = json.loads(body)
        self.assertEqual(data['expires'], '2023-11-14T22:14:20.000Z')     # toISOString()
        signal = SP.parse_sharing_signal(body)
        self.assertEqual((signal['action'], signal['id']), ('request', request_id))
        self.assertAlmostEqual(SP.parse_expires(signal['expires']), 1700000060.0)
        self.assertIsNone(SP.parse_sharing_signal(json.dumps({'action': 'request'})))          # no id
        self.assertEqual(json.loads(SP.build_request_reply('request_reject', 'x')), {'action': 'request_reject', 'id': 'x'})

    def test_expires(self):
        self.assertAlmostEqual(SP.parse_expires('2023-11-14T22:14:20.5Z'), 1700000060.5)
        self.assertAlmostEqual(SP.parse_expires('2023-11-14T22:14:20'), 1700000060.0)
        self.assertAlmostEqual(SP.parse_expires(1700000060500), 1700000060.5)
        self.assertIsNone(SP.parse_expires('tomorrow'))

    def test_pointer(self):
        self.assertEqual(SP.parse_pointer(SP.build_pointer(0.12345, 0.98765, 42)), (0.123, 0.988, 42))
        self.assertIsNone(SP.parse_pointer(json.dumps({'x': 1.5, 'y': 0})))
        self.assertEqual(SP.parse_pointer(json.dumps({'x': 0, 'y': 1})), (0, 1, None))
        self.assertEqual(SP.parse_ack(SP.build_ack(42)), 42)
        self.assertIs(SP.parse_visibility(json.dumps({'inApp': False})), False)
        self.assertIsNone(SP.parse_visibility(json.dumps({'inApp': 'no'})))

    def test_screen_devices(self):
        self.assertEqual(SP.screen_index('My screen'), 0)
        self.assertEqual(SP.screen_index('My screen 2'), 1)
        self.assertFalse(SP.is_screen_device('HD Webcam'))
        self.assertFalse(SP.is_screen_device(None))


class GeometryTests(unittest.TestCase):

    def test_view_to_frame(self):
        self.assertEqual(SP.view_to_frame(640, 360, 1280, 720, 1280, 720), (0.5, 0.5))
        # a 16:9 picture in a square view has bars above and below
        self.assertIsNone(SP.view_to_frame(500, 50, 1000, 1000, 1280, 720))
        nx, ny = SP.view_to_frame(0, 500 - 281.25, 1000, 1000, 1280, 720)
        self.assertAlmostEqual(nx, 0)
        self.assertAlmostEqual(ny, 0)

    def test_frame_to_view(self):
        x, y = SP.frame_to_view(1, 1, 1000, 1000, 1280, 720)
        self.assertAlmostEqual(x, 1000)
        self.assertAlmostEqual(y, 781.25)

    def test_frame_to_screen(self):
        # a 16:10 monitor pillarboxed in a 16:9 frame
        self.assertEqual(SP.frame_to_screen(0.5, 0.5, 1280, 720, 2560, 1600), (0.5, 0.5))
        self.assertIsNone(SP.frame_to_screen(0.01, 0.5, 1280, 720, 2560, 1600))
        sx, sy = SP.frame_to_screen(0.95, 0.0, 1280, 720, 2560, 1600)
        self.assertAlmostEqual(sx, 1)
        self.assertAlmostEqual(sy, 0)
        self.assertEqual(SP.frame_to_screen(0.3, 0.4, 1280, 720, 2560, 1600, letterboxed=False), (0.3, 0.4))


if __name__ == '__main__':
    unittest.main()
