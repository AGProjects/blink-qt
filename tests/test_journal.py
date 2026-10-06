"""Tests for blink.journal (dispatch rules and the page cache).

Run from the top of the tree: python3 -m unittest tests.test_journal
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest


def _load(name, filename):
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.message_envelopes', 'blink.journal')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
_load('blink.message_envelopes', 'message_envelopes.py')
journal = _load('blink.journal', 'journal.py')


def tearDownModule():
    for name, module in _saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class DispatchTests(unittest.TestCase):
    def test_actions(self):
        cases = {'message/imdn': 'receipt',
                 'application/sylk-conversation-remove': 'conversation_remove',
                 'application/sylk-message-remove': 'message_remove',
                 'application/sylk-conversation-read': 'conversation_read',
                 'text/pgp-public-key': 'public_key',
                 'text/pgp-private-key': 'ignored',
                 'application/sylk-addressbook-update': 'ignored',
                 'application/sylk-data-export': 'ignored',
                 'application/sylk-contact-update': 'ignored',
                 'application/im-iscomposing+xml': 'ignored',
                 'application/sylk-api-token': 'ignored',
                 'application/sylk-api-conversation-read': 'ignored',
                 'application/sylk-file-transfer': 'file_transfer',
                 'text/plain': 'text',
                 'TEXT/HTML': 'text',
                 'application/sylk-location-sharing': 'inert',
                 'application/sylk-message-metadata': 'inert',
                 'application/blink-call-detail-record': 'call_record',
                 'application/vnd.gsma.rcs-ft-http+xml': 'inert',
                 'application/x-future-thing': 'inert',
                 '': 'inert',
                 None: 'inert'}
        for content_type, expected in cases.items():
            self.assertEqual(journal.journal_action(content_type), expected, content_type)


class PayloadTests(unittest.TestCase):
    def test_payloads(self):
        self.assertEqual(journal.parse_payload('{"message_id": "m1"}'), {'message_id': 'm1'})
        self.assertEqual(journal.parse_payload(b'{"message_id": "m1"}'), {'message_id': 'm1'})
        self.assertEqual(journal.parse_payload("{'message_id': 'm1'}"), {'message_id': 'm1'})   # an old repr
        self.assertEqual(journal.parse_payload({'a': 1}), {'a': 1})
        for value in ("__import__('os').system('true')", '[1, 2]', 'garbage', '', None, 42):
            self.assertIsNone(journal.parse_payload(value), value)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.cache = journal.JournalCache(self.directory, max_attempts=3)

    def write(self, name, page):
        with open(os.path.join(self.directory, name), 'w') as page_file:
            page_file.write(page if isinstance(page, str) else json.dumps(page))

    def test_pages_sorted_and_applied(self):
        self.write('2026-10-06T10-00-00-000Z-b.json', {'cursor': 'a', 'messages': []})
        self.write('2026-10-05T10-00-00-000Z-a.json', {'cursor': '', 'messages': [{'message_id': 'a'}]})
        self.write('2026-10-07.json.part', '{}')
        self.assertEqual(self.cache.pages(), ['2026-10-05T10-00-00-000Z-a.json', '2026-10-06T10-00-00-000Z-b.json'])
        self.assertEqual(self.cache.load('2026-10-05T10-00-00-000Z-a.json')['messages'], [{'message_id': 'a'}])
        self.cache.applied('2026-10-05T10-00-00-000Z-a.json')
        self.assertEqual(self.cache.pages(), ['2026-10-06T10-00-00-000Z-b.json'])

    def test_bad_page(self):
        self.write('bad.json', 'not json')
        self.write('list.json', '[1, 2]')
        with self.assertRaises(ValueError):
            self.cache.load('bad.json')
        with self.assertRaises(ValueError):
            self.cache.load('list.json')

    def test_attempts_and_quarantine(self):
        self.write('p.json', 'not json')
        self.assertEqual(self.cache.failed('p.json'), (1, False))
        self.assertEqual(self.cache.failed('p.json'), (2, False))
        self.assertIn('attempts.json', os.listdir(self.directory))
        self.assertEqual(self.cache.pages(), ['p.json'])                 # attempts.json is not a page
        self.assertEqual(self.cache.failed('p.json'), (3, True))
        self.assertEqual(self.cache.pages(), [])
        self.assertTrue(os.path.exists(os.path.join(self.directory, 'quarantine', 'p.json')))
        self.assertNotIn('attempts.json', os.listdir(self.directory))  # nothing left to count

    def test_success_clears_attempts(self):
        self.write('p.json', {'cursor': '', 'messages': []})
        self.cache.failed('p.json')
        self.cache.applied('p.json')
        self.assertEqual(os.listdir(self.directory), [])

    def test_missing_directory(self):
        self.assertEqual(journal.JournalCache(os.path.join(self.directory, 'nope')).pages(), [])



class StatsTests(unittest.TestCase):
    def test_run(self):
        stats = journal.JournalStats('me@example.com', first_sync=True, reason='token received')
        stats.page_downloaded('p1.json', 4, 1000, 0.5, 'm4')
        stats.entry('text/plain', 'texts', 'alice@example.com', 'incoming', '2026-10-06T08:00:00Z')
        stats.entry('text/plain', 'texts', 'alice@example.com', 'outgoing', '2026-10-06T09:00:00Z')
        stats.entry('message/imdn', 'receipts skipped (first sync)', 'alice@example.com', 'incoming', '2026-10-06T08:30:00Z')
        stats.entry('application/x-future-thing', 'stored as application/x-future-thing', 'bob@example.com', 'incoming', '2026-10-05T10:00:00Z')
        stats.entry('application/sylk-location-sharing', 'stored as application/sylk-location-sharing', 'bob@example.com', 'incoming', None)
        stats.quarantined.append('bad.json')
        self.assertEqual(stats.entries, 5)
        self.assertEqual(stats.outcomes()['texts'], 2)
        self.assertEqual(stats.unhandled(), {'application/x-future-thing': 1})     # location is known, just stored for now
        data = stats.as_dict()
        self.assertEqual(data['conversations']['alice@example.com'], {'entries': 3, 'incoming': 2, 'types': {'text/plain': 2, 'message/imdn': 1},
                                                                       'first': '2026-10-06T08:00:00Z', 'last': '2026-10-06T09:00:00Z'})
        self.assertEqual(data['content_types']['text/plain'], {'received': 2, 'texts': 2})
        self.assertEqual(data['pages'], [{'file': 'p1.json', 'entries': 4, 'bytes': 1000, 'seconds': 0.5, 'cursor': 'm4'}])
        lines = stats.summary_lines()
        self.assertTrue(lines[0].startswith('Journal run of me@example.com: 5 entries, 1 pages'))
        self.assertIn('  text/plain: 2 received (2 texts)', lines)
        self.assertIn('  UNHANDLED application/x-future-thing x1', lines)
        self.assertIn('  QUARANTINED bad.json', lines)
        directory = tempfile.mkdtemp()
        path = stats.write(directory)
        self.assertTrue(os.path.basename(path).startswith('import-me@example.com-'))
        with open(path) as stats_file:
            self.assertEqual(json.load(stats_file)['entries'], 5)

    def test_top_conversations(self):
        stats = journal.JournalStats('me')
        for index in range(60):
            for _ in range(index + 1):
                stats.entry('text/plain', 'texts', f'c{index:02d}', 'incoming')
        lines = stats.summary_lines(top=50)
        self.assertIn('  60 conversations, the 50 largest:', lines)
        self.assertTrue(lines[3].startswith('    c59: 60 entries'))


class SeenMessageIdsTests(unittest.TestCase):
    def test_seen(self):
        seen = journal.SeenMessageIds(size=3)
        self.assertFalse(seen.seen('a'))
        self.assertTrue(seen.seen('a'))
        self.assertIn('a', seen)
        for value in (None, ''):
            self.assertFalse(seen.seen(value))
            self.assertFalse(seen.seen(value))
        self.assertEqual(len(seen), 1)

    def test_ring(self):
        seen = journal.SeenMessageIds(size=3)
        for message_id in 'abcd':
            self.assertFalse(seen.seen(message_id))
        self.assertEqual(len(seen), 3)
        self.assertNotIn('a', seen)                 # the oldest went
        self.assertTrue(seen.seen('d'))
        self.assertFalse(seen.seen('a'))

    def test_forget(self):
        seen = journal.SeenMessageIds()
        seen.seen('a')
        seen.forget('a')
        seen.forget('never seen')
        self.assertFalse(seen.seen('a'))


class OwnMarkersTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.markers = journal.OwnMarkers(ttl=30, clock=lambda: self.now)

    def test_device_id_settles_it(self):
        self.assertTrue(self.markers.is_echo('a@b', 'me', 'me'))
        self.assertFalse(self.markers.is_echo('a@b', 'phone', 'me'))
        self.markers.note('a@b')
        self.assertFalse(self.markers.is_echo('a@b', 'phone', 'me'))     # another device read it too

    def test_without_device_id(self):
        self.assertFalse(self.markers.is_echo('a@b', None, 'me'))
        self.markers.note('a@b')
        self.markers.note('a@b')
        self.assertTrue(self.markers.is_echo('a@b', None, 'me'))
        self.assertTrue(self.markers.is_echo('a@b', None, 'me'))         # one echo per send
        self.assertFalse(self.markers.is_echo('a@b', None, 'me'))

    def test_expiry(self):
        self.markers.note('a@b')
        self.now += 31
        self.assertFalse(self.markers.is_echo('a@b', None, 'me'))


if __name__ == '__main__':
    unittest.main()
