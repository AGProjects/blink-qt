"""Tests for blink.key_escrow. Run from the top of the tree: python3 -m unittest tests.test_key_escrow

The account storage (blink.key_storage) is replaced by a fake; lxml is required for the
old-attribute tests and the tests that encrypt or install a key are skipped without pgpy.
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


class FakeStorage(object):
    """What blink.key_storage.KeyStorage offers the escrow, in memory."""

    def __init__(self, state=None, record=None):
        self.state_ = state
        self.record_ = record
        self.stored = []

    def _wanted(self, account):
        return True

    def state(self, account):
        return self.state_

    def record(self, account):
        return dict(self.record_) if self.state_ == 'present' and self.record_ else None

    def writable(self, account):
        return self.state_ in ('present', 'absent')

    def store(self, account, payload, reason):
        if not self.writable(account):
            return False
        self.stored.append(json.loads(payload))
        self.state_, self.record_ = 'present', json.loads(payload)
        return True


def document(*contacts):
    """contacts: (id, [uris], {namespace: {name: value}})"""
    root = etree.Element('{urn:ietf:params:xml:ns:resource-lists}resource-lists')
    container = etree.SubElement(root, '{%s}contacts' % AB)
    for contact_id, uris, bags in contacts:
        contact = etree.SubElement(container, '{%s}contact' % AB, id=contact_id)
        uri_list = etree.SubElement(contact, '{%s}uris' % AB)
        for uri in uris:
            etree.SubElement(uri_list, '{%s}uri' % AB, id=uri, uri=uri.replace('@', '%40'))
        for namespace, attributes in (bags or {}).items():
            bag = etree.SubElement(contact, '{%s}attributes' % namespace)
            for name, value in attributes.items():
                etree.SubElement(bag, '{%s}attribute' % namespace, name=name).text = value
    return root


def account(account_id, private_key=None, public_key=None, password='secret', root=None):
    return types.SimpleNamespace(
        id=account_id,
        auth=types.SimpleNamespace(password=password),
        sms=types.SimpleNamespace(private_key=private_key, public_key=public_key),
        xcap=types.SimpleNamespace(enabled=True),
        xcap_manager=types.SimpleNamespace(resource_lists=types.SimpleNamespace(content=types.SimpleNamespace(element=root))),
        save=lambda: None)


def record(public_key='PUB-A', timestamp='2026-07-10T12:44:19.049Z'):
    return {'private_key': '-----BEGIN PGP MESSAGE-----', 'public_key': public_key,
            'device': 'phone', 'timestamp': timestamp}


class StorageTestCase(unittest.TestCase):
    def use(self, storage):
        self.storage = storage
        self.saved_key_storage = escrow._key_storage
        escrow._key_storage = lambda: storage

    def tearDown(self):
        if hasattr(self, 'saved_key_storage'):
            escrow._key_storage = self.saved_key_storage


class WriteRuleTests(StorageTestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.private = os.path.join(self.directory, 'a@x.privkey')
        self.public = os.path.join(self.directory, 'a@x.pubkey')
        with open(self.private, 'w') as f:
            f.write('PRIV')
        with open(self.public, 'w') as f:
            f.write('PUB-A\n')

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.directory)

    def test_blockers(self):
        self.use(FakeStorage('absent'))
        self.assertTrue(escrow.escrow_write_blockers(account('a@x', password=''), accounts={}))
        self.assertEqual(escrow.escrow_write_blockers(account('a@x', self.private, self.public), accounts={}), [])

    def test_storage_not_answered_is_refused(self):
        for state in (None, 'pending', 'failed', 'invalid', 'unsupported'):
            self.use(FakeStorage(state))
            blockers = escrow.escrow_write_blockers(account('a@x', self.private, self.public), accounts={})
            self.assertEqual(len(blockers), 1, state)
            self.assertIn('cannot be written', blockers[0])
            self.assertFalse(escrow.escrow_is_missing(account('a@x', self.private, self.public)), state)

    def test_escrow_of_another_account_is_refused(self):
        other = os.path.join(self.directory, 'b@x.pubkey')
        with open(other, 'w') as f:
            f.write('PUB-B')
        self.use(FakeStorage('present', record(public_key='PUB-B')))
        blockers = escrow.escrow_write_blockers(account('a@x', self.private, self.public), accounts={'a@x': self.public, 'b@x': other})
        self.assertIn('key of b@x', blockers[0])

    def test_actions(self):
        accounts = {'a@x': self.public}
        mine = account('a@x', self.private, self.public)
        self.use(FakeStorage('absent'))
        self.assertEqual(escrow.escrow_write_action(mine, accounts=accounts), (True, None))
        self.assertTrue(escrow.escrow_is_missing(mine))

        self.use(FakeStorage('present', record(public_key='PUB-A')))
        ok, reason = escrow.escrow_write_action(mine, accounts=accounts)
        self.assertFalse(ok)
        self.assertIn('already saved', reason)
        self.assertFalse(escrow.escrow_is_missing(mine))
        self.assertIsNone(escrow.foreign_escrow(mine))

        self.use(FakeStorage('present', record(public_key='PUB-OLD')))
        ok, reason = escrow.escrow_write_action(mine, accounts=accounts)
        self.assertFalse(ok)
        self.assertIn('does not hold', reason)
        self.assertEqual(escrow.escrow_write_action(mine, force=True, accounts=accounts), (True, None))
        self.assertIsNotNone(escrow.foreign_escrow(mine))

    def test_path_settings(self):
        class DataPath(str):
            @property
            def normalized(self):
                return str(self)
        self.assertTrue(escrow._has_file(DataPath(self.private)))
        self.assertFalse(escrow._has_file(None))


class OldAttributeTests(StorageTestCase):
    def test_finds_own_contacts_with_keys(self):
        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': json.dumps(record())}}),
                        ('c2', ['a@x'], None),
                        ('c3', ['b@x'], {SIPSIMPLE: {'keys': json.dumps(record())}}))
        self.assertEqual(escrow._own_contacts_with_keys(account('a@x', root=root)), ['c1'])

    def test_nil_attribute_is_not_an_escrow(self):
        root = document(('c1', ['a@x'], None))
        bag = etree.SubElement(root.find('.//{%s}contact' % AB), '{%s}attributes' % SIPSIMPLE)
        etree.SubElement(bag, '{%s}attribute' % SIPSIMPLE, name='keys', nil='true')
        self.assertEqual(escrow._own_contacts_with_keys(account('a@x', root=root)), [])

    def test_never_cleared_while_the_storage_keeps_none(self):
        root = document(('c1', ['a@x'], {SIPSIMPLE: {'keys': json.dumps(record())}}))
        for state in (None, 'absent', 'failed'):
            self.use(FakeStorage(state))
            self.assertEqual(escrow.strip_contact_escrow(account('a@x', root=root)), [], state)


@unittest.skipIf(pgpy is None, 'pgpy is not installed')
class KeyMaterialTests(StorageTestCase):
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
        super().tearDown()
        escrow.keys_directory = self.saved_directory
        shutil.rmtree(self.directory)

    def _write_local(self):
        private, public = os.path.join(self.directory, 'a@x.privkey'), os.path.join(self.directory, 'a@x.pubkey')
        with open(private, 'w') as f:
            f.write(self.private_key)
        with open(public, 'w') as f:
            f.write(self.public_key)
        return private, public

    def test_write_and_restore(self):
        private, public = self._write_local()
        self.use(FakeStorage('absent'))
        self.assertEqual(escrow.write_escrow(account('a@x', private, public), accounts={'a@x': public}), (True, None))
        built = self.storage.stored[-1]
        self.assertIn('BEGIN PGP MESSAGE', built['private_key'])
        self.assertTrue(built['timestamp'].endswith('Z'))
        self.assertIn('(Blink Qt)', built['device'])
        os.unlink(private)
        os.unlink(public)

        fresh = account('a@x')
        self.assertEqual(escrow.restore_from_storage(fresh), (True, None))
        self.assertEqual(fresh.sms.private_key, os.path.join(self.directory, 'a@x.privkey'))
        with open(fresh.sms.private_key) as f:
            self.assertEqual(f.read(), self.private_key.replace('\r', '').strip())
        ok, reason = escrow.restore_from_storage(fresh)
        self.assertFalse(ok)
        self.assertIn('already holds', reason)

    def test_reencrypt_after_password_change(self):
        private, public = self._write_local()
        mine = account('a@x', private, public, password='old')
        self.use(FakeStorage('present', escrow.escrow_record(mine)))
        self.assertEqual(escrow.escrow_password_check(mine), (False, None))
        mine.auth.password = 'new'
        self.assertEqual(escrow.escrow_password_check(mine), (True, None))
        self.assertEqual(escrow.reencrypt_escrow(mine, accounts={'a@x': public}), (True, None))
        self.assertEqual(escrow.escrow_password_check(mine), (False, None))

    def test_wrong_password_is_not_called_wrong(self):
        private, public = self._write_local()
        self.use(FakeStorage('present', escrow.escrow_record(account('a@x', private, public, password='old'))))
        ok, reason = escrow.restore_from_storage(account('a@x', password='new'))
        self.assertFalse(ok)
        self.assertIn('password changed', reason)

    def test_install_keeps_the_replaced_key(self):
        path = os.path.join(self.directory, 'a@x.privkey')
        with open(path, 'w') as f:
            f.write(self.private_key.replace('PRIVATE KEY BLOCK-----\n', 'PRIVATE KEY BLOCK-----\n\n', 1) + 'X')
        escrow.install_keypair(account('a@x'), self.private_key, self.public_key)
        kept = [name for name in os.listdir(self.directory) if name.startswith('a@x.privkey.')]
        self.assertEqual(len(kept), 1)

    def test_install_refuses_non_keys(self):
        with self.assertRaises(ValueError):
            escrow.install_keypair(account('a@x'), 'nope', self.public_key)


if __name__ == '__main__':
    unittest.main()
