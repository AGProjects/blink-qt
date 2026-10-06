"""Tests for blink.addressbook_origin. Run from the top of the tree: python3 -m unittest tests.test_addressbook_origin"""

import importlib.util
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


_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.addressbook_origin')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
origin = _load('blink.addressbook_origin', 'addressbook_origin.py')


def tearDownModule():
    for name, module in _saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def item(**kw):
    return types.SimpleNamespace(**kw)


def contact(name='Alice', uris=(('sip:alice@example.com', 'sip'),), attributes=None, id='c1'):
    return item(id=id, name=name, uris=[item(uri=uri, type=kind) for uri, kind in uris],
                presence=item(policy='allow', subscribe=True), dialog=None, attributes=attributes or {})


class FingerprintTests(unittest.TestCase):
    def test_contact(self):
        a = contact(uris=(('sip:a@x', 'sip'), ('+31201234567', 'tel')))
        b = contact(uris=(('+31201234567', 'tel'), ('sip:a@x ', 'sip')))       # order and padding ignored
        self.assertEqual(origin.contact_fingerprint(a), origin.contact_fingerprint(b))
        self.assertNotEqual(origin.contact_fingerprint(a), origin.contact_fingerprint(contact(name='Bob', uris=(('sip:a@x', 'sip'), ('+31201234567', 'tel')))))
        # attributes are not part of it
        self.assertEqual(origin.contact_fingerprint(contact(attributes={'organization': 'AG'})), origin.contact_fingerprint(contact()))
        # 'false' as XCAP text is false
        self.assertEqual(origin._event(item(policy=None, subscribe='false')), ['default', False])

    def test_group(self):
        self.assertEqual(origin.group_fingerprint(item(name='Calls', contacts=['b', 'a'])),
                         origin.group_fingerprint(item(name='Calls', contacts=[item(id='a'), item(id='b')])))


class ReasonTests(unittest.TestCase):
    def test_innermost_wins(self):
        self.assertEqual(origin.current_reason(), '')
        with origin.reason('backfill'):
            with origin.reason('call-history'):
                self.assertEqual(origin.current_reason(), 'call-history')
            self.assertEqual(origin.current_reason(), 'backfill')
        self.assertEqual(origin.current_reason(), '')

        @origin.with_reason('repair')
        def work():
            return origin.current_reason()
        self.assertEqual(work(), 'repair')


class StampTests(unittest.TestCase):
    def test_install_stamps_only_document_changes(self):
        saved = []

        class Contact(object):
            modified_by = modified_agent = modified_at = modified_reason = modified_hash = ''

            def __init__(self):
                self.id, self.name, self.__state__ = 'c1', 'Alice', 'active'
                self.__xcapcontact__ = None

            def __toxcap__(self):
                return contact(name=self.name)

            def save(self):
                saved.append(self.modified_hash)
                self.__xcapcontact__ = self.__toxcap__()

        class Group(Contact):
            pass

        origin._installed[0] = False
        origin.install(Contact, Group, device_id=lambda: 'dev1', agent=lambda: 'Blink Qt 6')
        entry = Contact()
        with origin.reason('repair'):
            entry.save()
        self.assertEqual((entry.modified_by, entry.modified_agent, entry.modified_reason), ('dev1', 'Blink Qt 6', 'repair'))
        self.assertTrue(entry.modified_at.endswith('Z'))
        first = entry.modified_at
        entry.modified_at = 'untouched'
        entry.save()                                   # nothing the document sees changed: no new stamp
        self.assertEqual(entry.modified_at, 'untouched')
        entry.name = 'Alice B'
        entry.save()
        self.assertNotEqual(entry.modified_at, 'untouched')
        self.assertEqual(entry.modified_reason, '')
        self.assertEqual(len(saved), 3)


class DiffTests(unittest.TestCase):
    def test_diff_and_snapshot(self):
        mine = contact(attributes={'modified_by': 'dev1', 'modified_agent': 'Blink', 'modified_at': 't'})
        mine.attributes['modified_hash'] = origin.contact_fingerprint(mine)
        document = item(contacts=[mine], groups=[])
        changes, snapshot = origin.diff_document(document, None, 'dev1')
        self.assertIsNone(changes)                      # first document is a baseline
        renamed = contact(name='Alice B', attributes=dict(mine.attributes))   # changed without a new stamp
        changes, _ = origin.diff_document(item(contacts=[renamed], groups=[]), snapshot, 'dev1')
        self.assertEqual(changes[0]['op'], 'update')
        self.assertIn('does not stamp', changes[0]['origin'])
        changes, _ = origin.diff_document(item(contacts=[], groups=[]), snapshot, 'dev1')
        self.assertEqual(changes[0]['op'], 'remove')
        self.assertTrue(origin.format_changes(changes)[0].startswith('remove contact c1'))
        path = os.path.join(tempfile.mkdtemp(), 'ab', 'snapshot.json')
        origin.save_snapshot(path, snapshot)
        self.assertEqual(origin.load_snapshot(path), snapshot)
        self.assertIsNone(origin.load_snapshot(path + '.missing'))


if __name__ == '__main__':
    unittest.main()
