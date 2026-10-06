"""Tests for blink.message_envelopes.

Run from the top of the tree: python3 -m unittest tests.test_envelopes
The module is loaded from its file so the tests need neither Qt nor sipsimple.
public_key_id is tested only when pgpy can be imported.
"""

import importlib.util
import json
import os
import unittest


_path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', 'message_envelopes.py')
_spec = importlib.util.spec_from_file_location('message_envelopes', _path)
env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(env)

try:
    import pgpy
except Exception:
    pgpy = None


def transfer(**kw):
    meta = {'filename': 'photo.jpg', 'filetype': 'image/jpeg', 'filesize': 2048, 'transfer_id': 't1', 'url': 'https://example.com/t1'}
    meta.update(kw)
    return json.dumps(meta)


RCS = '''<?xml version="1.0" encoding="UTF-8"?>
<file xmlns="urn:gsma:params:xml:ns:rcs:rcs:fthttp">
  <file-info type="thumbnail">
    <file-size>100</file-size><content-type>image/png</content-type>
    <data url="https://ft.example.com/thumb" until="2026-10-10T00:00:00Z"/>
  </file-info>
  <file-info type="file">
    <file-size>123456</file-size><file-name>holiday.mov</file-name><content-type>video/quicktime</content-type>
    <data url="https://ft.example.com/file?id=1" until="2026-10-10T00:00:00Z"/>
  </file-info>
</file>'''


class FileTransferTests(unittest.TestCase):
    def test_sylk_envelope(self):
        meta = env.file_transfer_envelope(transfer())
        self.assertEqual(meta['filename'], 'photo.jpg')
        meta['local'] = 'x'     # callers get a copy
        self.assertNotIn('local', env.file_transfer_envelope(transfer()))

    def test_rcs_envelope(self):
        meta = env.file_transfer_envelope(RCS)
        self.assertEqual(meta, {'filename': 'holiday.mov', 'filesize': 123456, 'filetype': 'video/quicktime',
                                'url': 'https://ft.example.com/file?id=1', 'until': '2026-10-10T00:00:00Z'})

    def test_not_a_transfer(self):
        for body in ('hello', '<p>hello</p>', '{"filename": ""}', '[1, 2]', None, b'\xff\xfe', '-----BEGIN PGP MESSAGE-----\nx\n-----END PGP MESSAGE-----'):
            self.assertIsNone(env.file_transfer_envelope(body), body)

    def test_category(self):
        cases = [(transfer(), 'image'),
                 (transfer(filename='clip.MOV', filetype='application/octet-stream'), 'video'),
                 (transfer(filename='note.m4a', filetype='video/mp4'), 'audio'),         # extension wins
                 (transfer(filename='rec.bin', filetype='video/mp4', call_recording=True), 'audio'),
                 (transfer(filename='rec.mp4', call_recording=True), 'video'),
                 (transfer(filename='photo.jpg.asc', filetype='application/octet-stream'), 'image'),
                 (transfer(filename='scan', filetype='image/tiff'), 'image'),
                 (transfer(filename='report.pdf', filetype='application/pdf'), 'other'),
                 (RCS, 'video'),
                 ('hello', None)]
        for body, expected in cases:
            self.assertEqual(env.file_transfer_category(body), expected, body)

    def test_summary(self):
        self.assertEqual(env.file_transfer_summary(transfer()), '\U0001F4CE photo.jpg\nImage · 2.0 KB')
        recording = transfer(filename='sylk-call-recording-1700000000000.ogg', filetype='audio/ogg', filesize=None, error='Download failed')
        self.assertEqual(env.file_transfer_summary(recording, duration=75), '\U0001F3A4 Call recording\n1:15\n⚠ Download failed')
        self.assertIsNone(env.file_transfer_summary('hello'))

    def test_recording_title(self):
        self.assertEqual(env.recording_title('sylk-audio-recording-1.m4a.asc'), 'Audio recording')
        self.assertIsNone(env.recording_title('interview.mp3'))

    def test_transfer_error(self):
        stored = transfer()
        failed = env.merge_transfer_error(stored, env.transfer_error_note('Not found'))
        self.assertEqual(json.loads(failed)['error'], 'Not found')
        self.assertIs(env.merge_transfer_error(failed, env.transfer_error_note('Not found')), failed)
        self.assertNotIn('error', json.loads(env.merge_transfer_error(failed, env.transfer_error_note(None))))
        self.assertIs(env.merge_transfer_error(stored, env.transfer_error_note(None)), stored)
        self.assertIs(env.merge_transfer_error(RCS, env.transfer_error_note('x')), RCS)


class PreviewTests(unittest.TestCase):
    def test_typed_text(self):
        self.assertEqual(env.conversation_preview('Hello\n  there', 'text/plain'), 'Hello there')
        self.assertEqual(env.conversation_preview('<p>Hi &amp; <b>bye</b></p>', 'text/html'), 'Hi & bye')
        self.assertEqual(env.conversation_preview('legacy', 'text'), 'legacy')

    def test_cut(self):
        preview = env.conversation_preview('x' * 150, 'text/plain')
        self.assertEqual(len(preview), env.CONVERSATION_PREVIEW_CHARS + 1)
        self.assertTrue(preview.endswith('…'))

    def test_passed_over(self):
        for body, content_type in (('key', 'text/pgp-public-key'),
                                   (transfer(), env.FILE_TRANSFER_CONTENT_TYPE),
                                   ('{}', env.LOCATION_CONTENT_TYPE),
                                   ('-----BEGIN PGP MESSAGE-----\nx\n-----END PGP MESSAGE-----', 'text/plain'),
                                   ('Audio call ended after 3 minutes', 'text/plain'),
                                   ('Public key received', 'text/plain'),
                                   ('\U0001F4CD 52.1, 4.3', 'text/plain'),
                                   ('Meeting request', 'text/plain'),
                                   ('Alice arrived at the meeting point', 'text/plain'),
                                   ('   ', 'text/plain'),
                                   (None, 'text/plain')):
            self.assertIsNone(env.conversation_preview(body, content_type), body)

    def test_reactions(self):
        self.assertIsNone(env.conversation_preview('\U0001F44D', 'text/plain', msgid='m1', reaction_ids={'m1'}))
        self.assertEqual(env.conversation_preview('\U0001F44D', 'text/plain', msgid='m2', reaction_ids={'m1'}), '\U0001F44D')
        self.assertEqual(env.conversation_preview('ok \U0001F44D', 'text/plain', msgid='m1', reaction_ids={'m1'}), 'ok \U0001F44D')

    def test_pure_emoji(self):
        self.assertTrue(env.is_pure_emoji('\U0001F44D\U0001F3FD ❤️'))
        self.assertTrue(env.is_pure_emoji('\U0001F468‍\U0001F469‍\U0001F467'))
        self.assertFalse(env.is_pure_emoji('ok'))
        self.assertFalse(env.is_pure_emoji(''))


class SidecarTests(unittest.TestCase):
    def test_reply_round_trip(self):
        body = env.reply_envelope('r1', 'o1', 'md1', 'alice@example.com', 1700000000)
        self.assertEqual(env.reply_metadata(body), {'reply_id': 'r1', 'original_id': 'o1', 'metadata_id': 'md1'})
        self.assertIsNone(env.reply_metadata(json.dumps({'action': 'reply', 'messageId': 'r1'})))
        self.assertIsNone(env.reply_metadata(env.label_envelope('t1', 'md', 'x', 'u', 1)))

    def test_label_round_trip(self):
        body = env.label_envelope('t1', 'md2', 'Sunset', 'alice@example.com', 1700000000)
        self.assertIn('"messageId":"t1"', body)       # removal matching relies on compact JSON
        self.assertEqual(env.label_metadata(body), {'transfer_id': 't1', 'label': 'Sunset', 'timestamp': '1700000000', 'metadata_id': 'md2'})
        self.assertEqual(env.label_metadata(env.label_envelope('t1', 'md3', '', 'u', 1))['label'], '')

    def test_peaks_round_trip(self):
        spectrum = {'data': 'AAAA', 'rate': 8000}
        body = env.peaks_envelope('t1', 'md4', {'l': [1, 2], 'r': [3]}, spectrum, 'alice@example.com', 1)
        self.assertEqual(env.peaks_metadata(body), {'transfer_id': 't1', 'peaks': {'l': [1, 2], 'r': [3]}, 'spectrum': spectrum})
        self.assertIsNone(env.peaks_metadata(env.peaks_envelope('t1', 'md5', {}, None, 'u', 1)))
        self.assertIsNone(env.peaks_metadata(env.peaks_envelope('t1', 'md6', {'l': [1]}, {'data': ''}, 'u', 1))['spectrum'])

    def test_call_recording_round_trip(self):
        encrypt = lambda text: '-----BEGIN PGP MESSAGE-----\n%s\n-----END PGP MESSAGE-----' % text
        decrypt = lambda armour: armour.split('\n')[1]
        body = env.call_recording_envelope('t1', 'bob@example.com', 'Bob', 61.234, 1700000000, encrypt=encrypt)
        self.assertNotIn('bob@example.com', json.loads(body)['value'].split('\n')[0])
        self.assertEqual(env.call_recording_metadata(body, decrypt=decrypt),
                         {'transfer_id': 't1', 'uri': 'bob@example.com', 'display_name': 'Bob', 'duration': 61.23})
        self.assertIsNone(env.call_recording_metadata(body))                     # no key
        self.assertIsNone(env.call_recording_envelope('t1', 'bob@example.com', 'Bob', 1, 1))   # cannot encrypt
        clear = json.dumps({'fileTransferId': 't1', 'action': 'call_recording', 'value': '{"uri": "bob@example.com"}', 'timestamp': '1'})
        self.assertIsNone(env.call_recording_metadata(clear, decrypt=decrypt))  # a party in clear is refused


class CategoryTests(unittest.TestCase):
    def test_categories(self):
        cases = [('text/plain', 'hi', 'text'),
                 ('text/html', '<b>hi</b>', 'text'),
                 ('text', 'hi', 'text'),
                 ('text/pgp-public-key', 'key', None),
                 ('text/pgp-private-key', 'key', None),
                 (env.CALL_CONTENT_TYPE, 'Call', 'call'),
                 (env.LEGACY_CALL_CONTENT_TYPE, '{}', 'call'),
                 (env.FILE_TRANSFER_CONTENT_TYPE, transfer(), 'image'),
                 (env.FILE_TRANSFER_CONTENT_TYPE, '-----BEGIN PGP MESSAGE-----\nx\n-----END PGP MESSAGE-----', None),
                 (env.RCS_FILE_TRANSFER_CONTENT_TYPE, RCS, 'video'),
                 (env.METADATA_CONTENT_TYPE, env.reply_envelope('r', 'o', 'm', 'u', 1), None),
                 (env.CONVERSATION_READ_CONTENT_TYPE, '{}', None),
                 ('', 'x', None),
                 (None, 'x', None)]
        for content_type, body, expected in cases:
            self.assertEqual(env.classify_category(content_type, body), expected, content_type)

    def test_location_without_location_module(self):
        # blink.location arrives in a later patch; until then a location is unclassified
        self.assertIsNone(env.classify_category(env.LOCATION_CONTENT_TYPE, '{}'))

    def test_category_names(self):
        self.assertEqual([key for key, _ in env.MESSAGE_CATEGORIES], ['text', 'links', 'audio', 'image', 'video', 'location', 'call', 'other'])

    def test_has_link(self):
        self.assertEqual(env.has_link('text/plain', 'see https://ag-projects.com'), 1)
        self.assertEqual(env.has_link('text/plain', 'call sip:alice@example.com'), 1)
        self.assertEqual(env.has_link('text/html', '<a href="http://example.com">x</a>'), 1)
        self.assertEqual(env.has_link('text/plain', 'no link, just example.com'), 0)
        self.assertEqual(env.has_link(env.FILE_TRANSFER_CONTENT_TYPE, transfer()), 0)
        self.assertEqual(env.has_link('text/plain', ''), 0)
        self.assertIsNone(env.has_link('text/plain', '-----BEGIN PGP MESSAGE-----\nhttps://x\n-----END PGP MESSAGE-----'))


@unittest.skipIf(pgpy is None, 'pgpy is not available')
class PublicKeyIdTests(unittest.TestCase):
    def test_key_id(self):
        from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm
        key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 1024)
        key.add_uid(pgpy.PGPUID.new('Test', email='test@example.com'), usage={KeyFlags.Sign, KeyFlags.EncryptCommunications},
                    hashes=[HashAlgorithm.SHA256], ciphers=[SymmetricKeyAlgorithm.AES256])
        armour = str(key.pubkey)
        expected = str(key.fingerprint).replace(' ', '')[-16:]
        self.assertEqual(env.public_key_id(armour), expected)
        self.assertEqual(env.public_key_id(armour.encode()), expected)
        # re-armoured with different line endings: same id
        self.assertEqual(env.public_key_id(armour.replace('\n', '\r\n')), expected)

    def test_not_a_key(self):
        self.assertIsNone(env.public_key_id('hello'))
        self.assertIsNone(env.public_key_id(None))


if __name__ == '__main__':
    unittest.main()
