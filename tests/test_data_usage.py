"""Tests for blink.data_usage. Run from the top of the tree: python3 -m unittest tests.test_data_usage
The modules are loaded from their files so the tests need neither Qt nor sipsimple."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

from datetime import datetime, timedelta, timezone


def _load(name):
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', name.split('.')[-1] + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.file_transfer', 'blink.message_envelopes', 'blink.data_usage')}
sys.modules['blink'] = types.ModuleType('blink')
_load('blink.file_transfer')
_load('blink.message_envelopes')
du = _load('blink.data_usage')
for _name, _module in _saved.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
ACCOUNT = 'alice@example.com'


def envelope(transfer_id, filename, filetype, size, until=None, url=True):
    meta = {'filename': filename, 'filetype': filetype, 'filesize': size, 'transfer_id': transfer_id}
    if url:
        meta['url'] = f'https://ft.example.com/filetransfer/{ACCOUNT}/bob@example.com/{transfer_id}/{filename}'
    if until:
        meta['until'] = until
    return json.dumps(meta)


class DataUsageTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.transfers = os.path.join(self.root, 'file_transfers')
        self.downloads = os.path.join(self.root, 'downloads')
        os.makedirs(self.downloads)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, peer, transfer_id, name, size, root=None):
        folder = os.path.join(root or os.path.join(self.transfers, ACCOUNT, peer), transfer_id)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'wb') as file:
            file.write(b'x' * size)
        return folder

    def rows(self, db_rows):
        folders = du.scan_local_folders(self.transfers, self.downloads, ACCOUNT)
        return du.build_local_rows(db_rows, folders, now=NOW)

    def test_matching_sizes_and_status(self):
        self.put('bob@example.com', 't1', 'clip.mp4', 3000)
        self.put('bob@example.com', 't2', 'old.mp4', 2000)
        self.put('carol@example.com', 't3', 'photo.jpg', 500)
        self.put('bob@example.com', 'orphan', 'left.mov', 700)
        self.put('bob@example.com', 't4', '.failure.json', 10)          # no file: not listed
        old = (NOW - timedelta(days=200)).strftime('%Y-%m-%d %H:%M:%S')
        recent = (NOW - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
        db_rows = [(1, 'm1', 'bob@example.com', '2026-10-26', recent, 'incoming', 'video', envelope('t1', 'clip.mp4', 'video/mp4', 3000)),
                   (2, 'm2', 'bob@example.com', '2026-04-12', old, 'incoming', 'video', envelope('t2', 'old.mp4', 'video/mp4', 2000, until='2026-05-01T00:00:00Z')),
                   (3, 'm3', 'carol@example.com', '2026-04-12', old, 'outgoing', 'image', envelope('t3', 'photo.jpg', 'image/jpeg', 500)),
                   (4, 'm4', 'bob@example.com', '2026-04-12', old, 'incoming', 'video', envelope('t9', 'missing.mp4', 'video/mp4', 9))]
        rows = self.rows(db_rows)
        by_name = {row.name: row for row in rows}
        self.assertEqual(set(by_name), {'clip.mp4', 'old.mp4', 'photo.jpg', 'left.mov'})
        self.assertEqual(by_name['clip.mp4'].action, du.ON_SERVER_TOO)
        self.assertEqual(by_name['old.mp4'].action, du.HERE_ONLY)          # expired on the server
        self.assertEqual(by_name['left.mov'].action, du.LEFTOVER)
        self.assertEqual(by_name['left.mov'].type, 'video')
        self.assertEqual(du.size_by(rows, 'contact')[0], ('bob@example.com', 5700, 3))
        videos_old = du.select_local(rows, kind='video', older_than=180, now=NOW)
        self.assertEqual([row.name for row in videos_old], ['old.mp4'])

    def test_downloads_layout_and_message_id(self):
        self.put(None, 'm5', 'doc.pdf', 100, root=self.downloads)
        rows = self.rows([(5, 'm5', 'bob@example.com', '2026-10-01', '2026-10-01 10:00:00', 'incoming', 'other', None)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, 'doc.pdf')
        self.assertEqual(rows[0].action, du.HERE_ONLY)

    def test_remove(self):
        kept = self.put('bob@example.com', 't1', 'clip.mp4', 3000)
        left = self.put('bob@example.com', 'orphan', 'left.mov', 700)
        rows = self.rows([(1, 'm1', 'bob@example.com', '2026-10-26', '2026-10-26 10:00:00', 'incoming', 'video', envelope('t1', 'clip.mp4', 'video/mp4', 3000))])
        message_ids, removed, freed, errors = du.remove_local_files(rows, [self.transfers, self.downloads], now=NOW)
        self.assertEqual((message_ids, removed, freed, errors), (['m1'], 2, 3700, []))
        self.assertEqual(os.listdir(kept), [du.REMOVED_MARKER])
        self.assertFalse(os.path.exists(left))
        self.assertEqual(self.rows([]), [])           # a folder with only the marker holds no file

    def test_remove_refuses_outside_storage(self):
        outside = tempfile.mkdtemp()
        try:
            with open(os.path.join(outside, 'mine.mp4'), 'wb') as file:
                file.write(b'x')
            row = du.LocalRow(1, 'm1', 'bob', '', '', NOW, '', 'video', du.HERE_ONLY, 'mine.mp4', 1, (('mine.mp4', 1),), outside, '', None)
            message_ids, removed, freed, errors = du.remove_local_files([row], [self.transfers])
            self.assertEqual((message_ids, removed), ([], 0))
            self.assertTrue(os.path.exists(os.path.join(outside, 'mine.mp4')))
        finally:
            shutil.rmtree(outside)


class FilesPurgeTest(unittest.TestCase):
    def test_payload_round_trip(self):
        payload = du.files_purge_payload('video', None, 'bob@example.com', 180, 'device-1', purge_id='p1', now=NOW)
        self.assertEqual(payload['criteria'], {'until': '2026-05-02T12:00:00.000Z', 'category': 'video', 'contact': 'bob@example.com'})
        criteria = du.files_purge_criteria(json.loads(json.dumps(payload)))
        self.assertEqual(criteria['until'], NOW - timedelta(days=180))
        self.assertEqual(criteria['category'], 'video')

    def test_payload_without_filters_is_until_now(self):
        payload = du.files_purge_payload(None, None, None, None, 'device-1', now=NOW)
        self.assertEqual(payload['criteria'], {'until': '2026-10-29T12:00:00.000Z'})

    def test_leftovers_are_not_announced(self):
        self.assertIsNone(du.files_purge_payload('video', du.LEFTOVER, None, None, 'device-1', now=NOW))

    def test_rejected(self):
        for criteria in ({}, {'until': 'soon'}, {'until': '2026-01-01T00:00:00Z', 'category': 'text'},
                         {'until': '2026-01-01T00:00:00Z', 'availability': 'leftover'}, {'until': '2026-01-01T00:00:00Z', 'ids': ['a']}):
            self.assertIsNone(du.files_purge_criteria({'criteria': criteria}), criteria)
        self.assertIsNone(du.files_purge_criteria(None))

    def test_matches(self):
        def row(name, kind, action, days):
            return du.LocalRow(1, 'm', 'bob', '', '', NOW - timedelta(days=days), '', kind, action, name, 1, (), '/x', '', None)
        rows = [row('a', 'video', du.ON_SERVER_TOO, 200), row('b', 'video', du.HERE_ONLY, 200), row('c', 'video', du.ON_SERVER_TOO, 10),
                row('d', 'image', du.ON_SERVER_TOO, 200), row('e', 'video', du.LEFTOVER, 200)]
        criteria = du.files_purge_criteria(du.files_purge_payload('video', None, None, 180, 'device-1', now=NOW))
        self.assertEqual([r.name for r in du.purge_matches(rows, criteria)], ['a', 'b'])
        criteria = du.files_purge_criteria(du.files_purge_payload('video', du.ON_SERVER_TOO, None, 180, 'device-1', now=NOW))
        self.assertEqual([r.name for r in du.purge_matches(rows, criteria)], ['a'])


if __name__ == '__main__':
    unittest.main()
