"""Tests for blink.pstn_normalize.

Run from the top of the tree: python3 -m unittest tests.test_pstn_normalize
The module is loaded from its file so the tests need neither Qt nor sipsimple.
"""

import importlib.util
import os
import unittest


_path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', 'pstn_normalize.py')
_spec = importlib.util.spec_from_file_location('pstn_normalize', _path)
pstn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pstn)


class _Settings(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _AccountId(str):
    @property
    def domain(self):
        return self.partition('@')[2]


def account(id='me@sip2sip.info', idd_prefix='00', prefix=None, replace_leading_zero='0031', strip_digits=None, conference=None):
    return _Settings(id=_AccountId(id),
                     pstn=_Settings(idd_prefix=idd_prefix, prefix=prefix, replace_leading_zero=replace_leading_zero, strip_digits=strip_digits),
                     conference=_Settings(server_address=conference))


NL = account()
IT = account(replace_leading_zero='0039')
US = account(idd_prefix='011', replace_leading_zero='0111')
NO_RULES = account(idd_prefix=None, replace_leading_zero=None)


class E164Tests(unittest.TestCase):
    def test_international_forms(self):
        for number in ('+31612345678', '0031612345678', 'sip:+31612345678@sip2sip.info', '+31 (0)6 1234 5678', '+31-6-1234-5678'):
            self.assertEqual(pstn.pstn_e164(number, NL), '+31612345678', number)

    def test_national_number_needs_replace_leading_zero(self):
        self.assertEqual(pstn.pstn_e164('0612345678', NL), '+31612345678')
        self.assertIsNone(pstn.pstn_e164('0612345678', NO_RULES))

    def test_trunk_zero_after_home_country_code(self):
        self.assertEqual(pstn.pstn_e164('+310237993800', NL), '+31237993800')
        self.assertEqual(pstn.pstn_e164('00310237993800', NL), '+31237993800')
        # only the home country code is repaired
        self.assertEqual(pstn.pstn_e164('+320237993800', NL), '+320237993800')

    def test_italy_keeps_its_zero(self):
        self.assertEqual(pstn.pstn_e164('0669820000', IT), '+390669820000')
        self.assertEqual(pstn.pstn_e164('+390669820000', IT), '+390669820000')

    def test_own_idd_prefix(self):
        self.assertEqual(pstn.pstn_e164('01131612345678', US), '+31612345678')
        # an international number on an 011 account is not a national one
        self.assertEqual(pstn.pstn_apply_leading_zero_rule('01131612345678', '0111', '011'), '01131612345678')

    def test_external_line_prefix(self):
        self.assertEqual(pstn.pstn_e164('90031612345678', account(prefix='9')), '+31612345678')
        # a real number that starts with the prefix digit is left alone
        self.assertEqual(pstn.pstn_e164('+9123456789', account(prefix='9')), '+9123456789')

    def test_not_phone_numbers(self):
        for value in ('alice@example.com', 'sip:alice@example.com', '1234', '+1234567', '', None, 'abc123'):
            self.assertIsNone(pstn.pstn_e164(value, NL), value)

    def test_minimum_length(self):
        self.assertIsNone(pstn.pstn_e164('+1234567', NL))
        self.assertEqual(pstn.pstn_e164('+12345678', NL), '+12345678')


class CanonicalUriTests(unittest.TestCase):
    def test_phone_number_becomes_bare_e164(self):
        self.assertEqual(pstn.canonical_pstn_uri('sip:0031612345678@sip2sip.info', NL), '+31612345678')

    def test_other_uris_are_lowercased(self):
        self.assertEqual(pstn.canonical_pstn_uri('sip:Alice@Example.COM', NL), 'alice@example.com')

    def test_anonymous(self):
        self.assertEqual(pstn.canonical_pstn_uri('sip:x7f3@guest.sip2sip.info', NL), pstn.ANONYMOUS_URI)
        self.assertEqual(pstn.canonical_pstn_uri('anonymous@anonymous.invalid', NL), pstn.ANONYMOUS_URI)
        self.assertEqual(pstn.normalize_anonymous_uri('alice@example.com'), 'alice@example.com')

    def test_none(self):
        self.assertEqual(pstn.canonical_pstn_uri(None, NL), '')


class SamePhoneNumberTests(unittest.TestCase):
    def test_spellings_match(self):
        self.assertTrue(pstn.same_phone_number('+31612345678', '0031612345678@gateway'))
        self.assertTrue(pstn.same_phone_number('+31 6 1234 5678', '0612345678'))
        self.assertTrue(pstn.same_phone_number('sip:+31612345678@sip2sip.info', '+31612345678'))

    def test_short_or_different_numbers_do_not_match(self):
        self.assertFalse(pstn.same_phone_number('1233', '91233'))  # too short for a tail match
        self.assertFalse(pstn.same_phone_number('+31612345678', '+31612345679'))
        self.assertFalse(pstn.same_phone_number('alice@example.com', 'alice@example.com'))


class SpellingsTests(unittest.TestCase):
    def test_all_spellings_of_a_number(self):
        spellings = pstn.pstn_uri_spellings('+31612345678', NL)
        self.assertEqual(spellings[0], '+31612345678')
        for expected in ('0031612345678', '0612345678', '+31612345678@sip2sip.info', '0031612345678@sip2sip.info', '0612345678@sip2sip.info'):
            self.assertIn(expected, spellings)

    def test_non_numbers_have_one_spelling(self):
        self.assertEqual(pstn.pstn_uri_spellings('sip:alice@example.com', NL), ['alice@example.com'])


class DialUsernameTests(unittest.TestCase):
    def test_wire_form(self):
        self.assertEqual(pstn.pstn_dial_username('06 1234 5678', '00', None, None, '0031'), '0031612345678')
        self.assertEqual(pstn.pstn_dial_username('+31612345678', '00'), '0031612345678')
        self.assertEqual(pstn.pstn_dial_username('+31612345678', '00', prefix='9'), '90031612345678')


class ConferenceTests(unittest.TestCase):
    def test_conference_domains(self):
        self.assertTrue(pstn.is_conference_uri('room1@conference.sip2sip.info'))
        self.assertTrue(pstn.is_conference_uri('sip:338318@videoconference.sylk.link'))
        self.assertTrue(pstn.is_conference_uri('room@bridge.example.com', account(conference='bridge.example.com')))
        self.assertFalse(pstn.is_conference_uri('alice@sip2sip.info'))
        self.assertFalse(pstn.is_conference_uri('room1'))


if __name__ == '__main__':
    unittest.main()
