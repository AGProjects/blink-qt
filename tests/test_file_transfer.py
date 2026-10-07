"""Tests for blink.file_transfer. Run from the top of the tree: python3 -m unittest tests.test_file_transfer"""

import importlib.util
import os
import unittest


def _load():
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', 'file_transfer.py')
    spec = importlib.util.spec_from_file_location('file_transfer_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ft = _load()

BASE = 'https://webrtc-gateway.sipthor.net:9999/webrtcgateway/filetransfer'


class URLTests(unittest.TestCase):
    def test_upload_url(self):
        self.assertEqual(ft.upload_url(BASE + '/', 'a@x', 'b@y', 'id1', 'photo.jpg'), BASE + '/a@x/b@y/id1/photo.jpg')

    def test_base_from_transfer(self):
        self.assertEqual(ft.base_url_from_transfer(BASE + '/a@x/b@y/id1/photo.jpg?x=1'), BASE)
        self.assertIsNone(ft.base_url_from_transfer('https://example.com/files/a/b/c/d'))   # not /filetransfer
        self.assertIsNone(ft.base_url_from_transfer('https://example.com/filetransfer/a'))  # too short
        self.assertIsNone(ft.base_url_from_transfer(''))
        self.assertIsNone(ft.base_url_from_transfer(None))

    def test_over_encoded(self):
        encoded = 'https%3A//webrtc-gateway.sipthor.net%3A9999/webrtcgateway/filetransfer/a@x/b@y/id1/a%3Ab.txt'
        self.assertEqual(ft.normalized_url(encoded), 'https://webrtc-gateway.sipthor.net:9999/webrtcgateway/filetransfer/a@x/b@y/id1/a%3Ab.txt')
        self.assertEqual(ft.base_url_from_transfer(encoded), BASE)
        self.assertEqual(ft.normalized_url(BASE), BASE)       # already fine: untouched

    def test_derive(self):
        self.assertEqual(ft.derive_base_url('https://webrtc-gateway.sipthor.net:9999/webrtcgateway/messages/history/a@x'), BASE)
        self.assertIsNone(ft.derive_base_url('https://example.com/journal/a@x'))
        self.assertIsNone(ft.derive_base_url(None))


if __name__ == '__main__':
    unittest.main()
