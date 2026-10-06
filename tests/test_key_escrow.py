"""Tests for blink.key_escrow. Run from the top of the tree: python3 -m unittest tests.test_key_escrow

lxml is required; the tests that encrypt or install a key are skipped without pgpy.
"""

import importlib.util
import json
import os
import shutil
import tempfile
import types
import unittest

from lxml import etree


def _load():
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', 'key_escrow.py')
    spec = importlib.util.spec_from_file_location('key_escrow_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


escrow = _load()
pgpy = escrow._pgpy()

AB = escrow.ADDRESSBOOK_NS
SIPSIMPLE = escrow.SIPSIMPLE_ATTRIBUTES_NS
BLINK = escrow.BLINK_ATTRIBUTES_NS


def document(*contacts):
    """contacts: (id, [uris], {namespace: {name: value}})"""
    root = etree.Element('{urn:ietf:params:xml:ns:resource-lists}resource-lists')
    container = etree.SubElement(root, '{%s}contacts' % AB)
    for contact_id, uris, bags in contacts:
        contact = etree.SubElement(container, '{%s}contact' % AB, id=contact_id)
        etree.SubElement(contact, '{%s}name' % AB).text = contact_id
        uri_list = etree.SubElement(contact, '{%s}uris' % AB)
        for uri in uris:
            uri_element = etree.SubElement(uri_list, '{%s}uri' % AB, id=uri, uri=uri.replace('@', '%40'))
            # a URI's own bag must not be read as the contact's
            uri_bag = etree.SubElement(uri_element, '{%s}attributes' % SIPSIMPLE)
            etree.SubElement(uri_bag, '{%s}attribute' % SIPSIMPLE, name='position').text = '1'
        for namespace, attributes in (bags or {}).items():
            bag = etree.SubElement(contact, '{%s}attributes' % namespace)
            for name, value in attributes.items():
                etree.SubElement(bag, '{%s}attribute' % namespace, name=name).text = value
    return root


def account(account_id, root, private_key=None, public_key=None, password='secret'):
    return types.SimpleNamespace(
        id=account_id,
        auth=types.SimpleNamespace(password=password),
        sms=types.SimpleNamespace(private_key=private_key, public_key=public_key),
        xcap_manager=types.SimpleNamespace(resource_lists=types.SimpleNamespace(content=types.SimpleNamespace(element=root))),
        save=lambda: None)


def record(public_key='PUB', timestamp='2026-01-01T00:00:00.000Z', device='phone'):
    return json.dumps({'private_key': '-----BEGIN PGP MESSAGE-----x', 'public_key': public_key,
                       'device': device, 'timestamp': timestamp})


class ReadTests(unittest.TestCase):
    def test_no_document(self):
        self.assertEqual(escrow.self_contact_elements(account('a@x', None)), [])
        self.assertIsNone(escrow.read_self_keys(account('a@x', None)))

    def test_finds_self_contacts_by_decoded_uri(self):
        root = document(('c1', ['a@x'], None), ('c2', ['b@x'], None), ('c3', ['A@X'], None))
        ids = [element.get('id') for element in escrow.self_contact_elements(account('a@x', root))]
        self.assertEqual(ids, ['c1', 'c3'])
        self.assertEqual(escrow.escrow_write_targets(account('a@x', root)), ['c1', 'c3'])

    def test_contact_bag_only(self):
        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': record()}}))
        element = escrow.self_contact_element(account('a@x', root))
        self.assertEqual(sorted(escrow._attributes(element, SIPSIMPLE)), ['keys'])

    def test_newest_wins_across_contacts_and_bags(self):
        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': record(timestamp='2026-01-01T00:00:00.000Z')}}),
                        ('c2', ['a@x'], {BLINK: {'keys': record(timestamp='2026-02-01T00:00:00.000Z', device='mac')}}))
        found = escrow.read_self_keys(account('a@x', root))
        self.assertEqual((found['contact_id'], found['namespace'], found['device']), ('c2', 'blink', 'mac'))

    def test_bad_json_is_skipped(self):
        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': 'not json'}}), ('c2', ['a@x'], {SIPSIMPLE: {'keys': '[1]'}}))
        self.assertIsNone(escrow.read_self_keys(account('a@x', root)))


class WriteRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.private = os.path.join(self.directory, 'a@x.privkey')
        self.public = os.path.join(self.directory, 'a@x.pubkey')
        with open(self.private, 'w') as f:
            f.write('PRIV')
        with open(self.public, 'w') as f:
            f.write('PUB-A\n')

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_blockers(self):
        self.assertTrue(escrow.escrow_write_blockers(account('a@x', None, password=''), accounts={}))
        root = document(('c1', ['b@x'], None))
        blockers = escrow.escrow_write_blockers(account('a@x', root, self.private, self.public), accounts={})
        self.assertIn('no contact carries the URI', blockers[0])

    def test_contact_shared_with_another_account_is_refused(self):
        root = document(('c1', ['a@x', 'b@x'], None))
        blockers = escrow.escrow_write_blockers(account('a@x', root, self.private, self.public), accounts={'a@x': self.public, 'b@x': None})
        self.assertEqual(len(blockers), 1)
        self.assertIn('also carries b@x', blockers[0])

    def test_escrow_of_another_account_is_refused(self):
        other = os.path.join(self.directory, 'b@x.pubkey')
        with open(other, 'w') as f:
            f.write('PUB-B')
        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': record(public_key='PUB-B')}}))
        blockers = escrow.escrow_write_blockers(account('a@x', root, self.private, self.public), accounts={'a@x': self.public, 'b@x': other})
        self.assertIn('key of b@x', blockers[0])

    def test_actions(self):
        accounts = {'a@x': self.public}
        mine = account('a@x', document(('c1', ['a@x'], None)), self.private, self.public)
        self.assertEqual(escrow.escrow_write_action(mine, accounts=accounts), (True, None))
        self.assertTrue(escrow.escrow_is_missing(mine))

        saved = account('a@x', document(('c1', ['a@x'], {SIPSIMPLE: {'keys': record(public_key='PUB-A')}})), self.private, self.public)
        ok, reason = escrow.escrow_write_action(saved, accounts=accounts)
        self.assertFalse(ok)
        self.assertIn('already saved', reason)
        self.assertFalse(escrow.escrow_is_missing(saved))

        partial = account('a@x', document(('c1', ['a@x'], {SIPSIMPLE: {'keys': record(public_key='PUB-A')}}), ('c2', ['a@x'], None)), self.private, self.public)
        self.assertEqual(escrow.escrow_write_action(partial, accounts=accounts), (True, None))

        foreign = account('a@x', document(('c1', ['a@x'], {SIPSIMPLE: {'keys': record(public_key='PUB-OLD')}})), self.private, self.public)
        ok, reason = escrow.escrow_write_action(foreign, accounts=accounts)
        self.assertFalse(ok)
        self.assertIn('does not hold', reason)
        self.assertEqual(escrow.escrow_write_action(foreign, force=True, accounts=accounts), (True, None))

    def test_path_settings(self):
        class DataPath(str):
            @property
            def normalized(self):
                return str(self)
        self.assertTrue(escrow._has_file(DataPath(self.private)))
        self.assertFalse(escrow._has_file(None))


@unittest.skipIf(pgpy is None, 'pgpy is not installed')
class KeyMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm
        key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 1024)
        key.add_uid(pgpy.PGPUID.new('a@x'), usage={KeyFlags.EncryptCommunications}, hashes=[HashAlgorithm.SHA256], ciphers=[SymmetricKeyAlgorithm.AES256])
        cls.private_key = str(key)
        cls.public_key = str(key.pubkey)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.saved_directory = escrow.keys_directory
        escrow.keys_directory = lambda: self.directory

    def tearDown(self):
        escrow.keys_directory = self.saved_directory
        shutil.rmtree(self.directory)

    def _write_local(self):
        private, public = os.path.join(self.directory, 'a@x.privkey'), os.path.join(self.directory, 'a@x.pubkey')
        with open(private, 'w') as f:
            f.write(self.private_key)
        with open(public, 'w') as f:
            f.write(self.public_key)
        return private, public

    def test_record_round_trip_and_restore(self):
        private, public = self._write_local()
        built = escrow.escrow_record(account('a@x', None, private, public))
        self.assertIn('BEGIN PGP MESSAGE', built['private_key'])
        self.assertTrue(built['timestamp'].endswith('Z'))
        self.assertIn('(Blink Qt)', built['device'])
        os.unlink(private)
        os.unlink(public)

        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': json.dumps(built)}}))
        fresh = account('a@x', root)
        self.assertEqual(escrow.restore_from_own_contact(fresh), (True, None))
        self.assertEqual(fresh.sms.private_key, os.path.join(self.directory, 'a@x.privkey'))
        with open(fresh.sms.private_key) as f:
            self.assertEqual(f.read(), self.private_key.replace('\r', '').strip())
        ok, reason = escrow.restore_from_own_contact(fresh)
        self.assertFalse(ok)
        self.assertIn('already holds', reason)

    def test_wrong_password_is_not_called_wrong(self):
        private, public = self._write_local()
        built = escrow.escrow_record(account('a@x', None, private, public, password='old'))
        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': json.dumps(built)}}))
        ok, reason = escrow.restore_from_own_contact(account('a@x', root, password='new'))
        self.assertFalse(ok)
        self.assertIn('password changed', reason)

    def test_install_keeps_the_replaced_key(self):
        path = os.path.join(self.directory, 'a@x.privkey')
        with open(path, 'w') as f:
            f.write(self.private_key.replace('PRIVATE KEY BLOCK-----\n', 'PRIVATE KEY BLOCK-----\n\n', 1) + 'X')
        escrow.install_keypair(account('a@x', None), self.private_key, self.public_key)
        kept = [name for name in os.listdir(self.directory) if name.startswith('a@x.privkey.')]
        self.assertEqual(len(kept), 1)

    def test_install_refuses_non_keys(self):
        with self.assertRaises(ValueError):
            escrow.install_keypair(account('a@x', None), 'nope', self.public_key)


if __name__ == '__main__':
    unittest.main()
