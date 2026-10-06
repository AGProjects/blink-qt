"""Tests for blink.uris.

Run from the top of the tree: python3 -m unittest tests.test_uris
The modules are loaded from their files so the tests need neither Qt nor
sipsimple (illegal_uri then uses its fallback shape check).
"""

import importlib.util
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


# blink/__init__.py pulls in Qt; give uris.py a bare package to import from
_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.pstn_normalize', 'blink.uris')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
_load('blink.pstn_normalize', 'pstn_normalize.py')
uris = _load('blink.uris', 'uris.py')
for _name, _module in _saved.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


class _Settings(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


NL = _Settings(pstn=_Settings(idd_prefix='00', prefix=None, replace_leading_zero='0031', strip_digits=None))
NO_RULES = _Settings(pstn=_Settings(idd_prefix=None, prefix=None, replace_leading_zero=None, strip_digits=None))

UUID = '0aa407ca-4c53-46b6-ab15-f31d0b77c3ac'


class CanonicalUriTests(unittest.TestCase):
    def test_sip_address_forms(self):
        for value in ('alice@example.com', 'sip:alice@example.com', 'SIP:Alice@Example.COM', 'sips:alice@example.com',
                      'sip:alice@example.com;transport=tls', 'sip:alice@example.com?subject=hi',
                      '"Alice" <sip:alice@example.com;transport=tcp>', '  alice@example.com  ', b'sip:alice@example.com'):
            self.assertEqual(uris.canonical_uri(value, NL), 'alice@example.com', value)

    def test_port_is_kept(self):
        self.assertEqual(uris.canonical_uri('sip:alice@example.com:5061', NL), 'alice@example.com:5061')

    def test_phone_number_spellings_are_one_key(self):
        for value in ('+31612345678', '0031612345678', '0612345678', 'sip:+31612345678@sip2sip.info',
                      'sip:0031612345678@sip2sip.info;user=phone', 'tel:+31612345678', '+31 (0)6 1234 5678',
                      '<sip:0612345678@sip2sip.info>'):
            self.assertEqual(uris.canonical_uri(value, NL), '+31612345678', value)

    def test_national_number_without_rules_stays_as_is(self):
        self.assertEqual(uris.canonical_uri('0612345678', NO_RULES), '0612345678')

    def test_anonymous(self):
        self.assertEqual(uris.canonical_uri('sip:x7f3@guest.sip2sip.info', NL), 'anonymous@anonymous.invalid')

    def test_instance_id(self):
        self.assertEqual(uris.canonical_uri('urn:uuid:%s' % UUID.upper(), NL), UUID)
        self.assertEqual(uris.canonical_uri(UUID, NL), UUID)

    def test_empty(self):
        for value in (None, '', '   ', 'sip:', '<>'):
            self.assertEqual(uris.canonical_uri(value, NL), '', value)


class InstanceIdTests(unittest.TestCase):
    def test_bare(self):
        self.assertEqual(uris.bare_instance_id('urn:uuid:%s' % UUID), UUID)
        self.assertEqual(uris.bare_instance_id('URN:UUID:%s' % UUID), UUID)
        self.assertEqual(uris.bare_instance_id(' %s ' % UUID), UUID)
        self.assertEqual(uris.bare_instance_id(None), '')


class PlaceholderTests(unittest.TestCase):
    def test_placeholders(self):
        for value in ('sip:%s@bonjour.local' % UUID, '%s@127.0.0.1' % UUID, 'sip:x@localhost:5060', 'SIPS:x@Bonjour.Local;transport=tls'):
            self.assertTrue(uris.is_placeholder_uri(value), value)

    def test_real_addresses(self):
        for value in ('alice@example.com', 'sip:alice@example.local', '+31612345678', None):
            self.assertFalse(uris.is_placeholder_uri(value), value)


class IllegalUriTests(unittest.TestCase):
    def test_illegal(self):
        for value in ('338318@videoconference.sylk.link', 'x7f3@guest.sip2sip.info', 'X@Guest.example.com', '', None, '@example.com', 'alice @example.com'):
            self.assertTrue(uris.illegal_uri(value), value)

    def test_legal(self):
        for value in ('alice@example.com', 'sip:alice@example.com', '+31612345678@sip2sip.info', 'room1@conference.sip2sip.info'):
            self.assertFalse(uris.illegal_uri(value), value)


class FileableTests(unittest.TestCase):
    def test_addresses_and_numbers(self):
        for value in ('alice@example.com', '+31612345678', '0031612345678', '0612345678', '+31612345678@sip2sip.info'):
            self.assertTrue(uris.is_fileable_address(value, NL), value)

    def test_not_fileable(self):
        for value in (UUID, 'alice', '', None, 'sip:%s@bonjour.local' % UUID, '%s@127.0.0.1' % UUID):
            self.assertFalse(uris.is_fileable_address(value, NL), value)

    def test_bonjour_neighbour(self):
        keys = frozenset([uris.canonical_uri('neighbour@192.168.1.20', NL)])
        self.assertFalse(uris.is_fileable_address('sip:Neighbour@192.168.1.20;transport=tcp', NL, keys))
        self.assertTrue(uris.is_fileable_address('alice@example.com', NL, keys))


if __name__ == '__main__':
    unittest.main()
