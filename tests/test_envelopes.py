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

    def test_metadata_link(self):
        armour = lambda text: '-----BEGIN PGP MESSAGE-----\n%s\n-----END PGP MESSAGE-----' % text
        self.assertEqual(env.metadata_link(env.reply_envelope('r1', 'o1', 'md1', 'u', 1)), ('r1', 'reply'))
        self.assertEqual(env.metadata_link(env.label_envelope('t1', 'md2', 'Sunset', 'u', 1)), ('t1', 'label'))
        self.assertEqual(env.metadata_link(env.peaks_envelope('t2', 'md3', {'l': [1]}, None, 'u', 1).encode()), ('t2', 'peaks'))
        self.assertEqual(env.metadata_link(env.call_recording_envelope('t3', 'bob@example.com', '', 0, 1, encrypt=armour)), ('t3', 'call_recording'))
        for body in (None, '', 'garbage', '[1]', json.dumps({'action': 'reply'}), json.dumps({'action': 'reply', 'messageId': ' '}),
                     json.dumps({'action': 'mystery', 'messageId': 'x'}), json.dumps({'action': 'label', 'messageId': {'a': 1}}),
                     armour('{"action": "label", "messageId": "x"}')):
            self.assertIsNone(env.metadata_link(body), body)


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

    def test_location_without_action(self):
        # loaded standalone blink.location is not importable, and an envelope without an action is unclassified anyway
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


class CallRecordTests(unittest.TestCase):
    def test_build(self):
        import datetime
        record = env.build_call_record('s1', 'outgoing', 'completed', duration=65, status=200, remote_party='bob@example.com',
                                       start_time=datetime.datetime(2026, 9, 8, 12, 0, 0), stop_time='2026-09-08 12:01:05',
                                       media=['audio', 'video'], display_name='', proxy_ip=None)
        self.assertEqual(record, {'version': 1, 'sessionId': 's1', 'direction': 'outgoing', 'outcome': 'completed', 'duration': 65,
                                  'remoteParty': 'bob@example.com', 'source': 'local', 'status': '200',
                                  'startTime': '2026-09-08T12:00:00+00:00', 'stopTime': '2026-09-08T12:01:05+00:00',
                                  'media': ['audio', 'video']})

    def test_call_time_keeps_offsets(self):
        import datetime
        aware = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
        self.assertEqual(env._call_time(aware), '2026-09-08T12:00:00+02:00')
        for text in ('2026-09-08T12:00:00Z', '2026-09-08T12:00:00+02:00', '2026-09-08T12:00:00-05:00'):
            self.assertEqual(env._call_time(text), text)
        self.assertIsNone(env._call_time(''))

    def test_merge_ranks(self):
        missed_here = env.build_call_record('s1', 'incoming', 'missed', remote_party='bob@example.com', local={'deviceId': 'me'})
        answered = env.build_call_record('s1', 'incoming', 'completed', duration=30, answered_by='phone', source='device')
        server = env.build_call_record('s1', 'incoming', 'completed', duration=31, proxy_ip='1.2.3.4', source='server')

        merged = env.merge_call_records(missed_here, answered)
        self.assertEqual((merged['outcome'], merged['duration'], merged['source']), ('completed', 30, 'device'))
        self.assertEqual(merged['local'], {'deviceId': 'me'})              # never erased

        # a lower ranked view cannot undo a higher one, but may add what it alone knows
        again = env.merge_call_records(merged, env.build_call_record('s1', 'incoming', 'missed', display_name='Bob'))
        self.assertEqual((again['outcome'], again['duration'], again['source'], again['displayName']), ('completed', 30, 'device', 'Bob'))

        final = env.merge_call_records(again, server)
        self.assertEqual((final['duration'], final['proxyIP'], final['source']), (31, '1.2.3.4', 'server'))
        self.assertEqual(env.merge_call_records(final, answered)['source'], 'server')
        self.assertEqual(env.merge_call_records(final, answered)['duration'], 31)

        # equal rank updates normally
        first = env.build_call_record('s2', 'outgoing', 'failed')
        self.assertEqual(env.merge_call_records(first, env.build_call_record('s2', 'outgoing', 'completed', duration=5))['outcome'], 'completed')
        self.assertIs(env.merge_call_records(None, first), first)
        self.assertIs(env.merge_call_records(first, None), first)

    def test_migrated_rank_is_lowest(self):
        local = env.build_call_record('s1', 'incoming', 'completed', duration=10)
        migrated = env.build_call_record('s1', 'incoming', 'missed', source='migrated')
        self.assertEqual(env.merge_call_records(local, migrated)['outcome'], 'completed')

    def test_call_record_lookup(self):
        record = env.build_call_record('s1', 'incoming', 'missed')
        self.assertEqual(env.call_record('Missed call', json.dumps(record)), record)
        self.assertEqual(env.call_record(json.dumps(record).encode()), record)
        self.assertIsNone(env.call_record('Missed call', '{"no": "session"}'))
        self.assertIsNone(env.call_record(None))

    def test_outcome_derivation(self):
        self.assertEqual(env.call_outcome({'direction': 'incoming', 'duration': 3}), 'completed')
        self.assertEqual(env.call_outcome({'direction': 'outgoing', 'status': '487'}), 'cancelled')
        self.assertEqual(env.call_outcome({'direction': 'outgoing', 'status': '486'}), 'failed')
        self.assertEqual(env.call_outcome({'direction': 'incoming'}), 'missed')

    def test_missed_and_attention(self):
        self.assertTrue(env.call_was_missed({'direction': 'incoming', 'outcome': 'voicemail'}))
        self.assertFalse(env.call_was_missed({'direction': 'outgoing', 'outcome': 'missed'}))
        self.assertFalse(env.call_was_missed(None))
        elsewhere = {'direction': 'incoming', 'outcome': 'completed', 'answeredBy': 'phone', 'duration': 5}
        self.assertFalse(env.call_needs_attention(elsewhere, 'laptop'))
        self.assertTrue(env.call_needs_attention({'direction': 'outgoing', 'outcome': 'failed'}))
        self.assertFalse(env.call_needs_attention({'direction': 'outgoing', 'outcome': 'cancelled'}))

    def test_summary(self):
        cases = [({'direction': 'incoming', 'outcome': 'completed', 'duration': 3725}, None, 'Incoming call (1:02:05)'),
                 ({'direction': 'incoming', 'outcome': 'completed', 'duration': 65, 'answeredBy': 'phone'}, 'laptop', 'Answered on another device (1:05)'),
                 ({'direction': 'incoming', 'outcome': 'completed', 'duration': 65, 'answeredBy': 'laptop'}, 'laptop', 'Incoming call (1:05)'),
                 ({'direction': 'outgoing', 'outcome': 'failed', 'status': '486', 'reason': 'Busy'}, None, 'Call failed — Busy Here (486)'),
                 ({'direction': 'outgoing', 'outcome': 'failed', 'status': '499', 'reason': 'Odd'}, None, 'Call failed — Odd'),
                 ({'direction': 'outgoing', 'outcome': 'failed', 'status': '499'}, None, 'Call failed — 499'),
                 ({'direction': 'outgoing', 'outcome': 'failed'}, None, 'Call failed'),
                 ({'direction': 'incoming', 'outcome': 'missed', 'media': ['audio', 'video']}, None, 'Missed video call'),
                 ({'direction': 'incoming', 'outcome': 'completed', 'duration': 9, 'media': ['video'], 'local': {'streams': ['audio']}}, None, 'Incoming call (0:09)'),
                 ({'direction': 'outgoing', 'outcome': 'cancelled'}, None, 'Cancelled call')]
        for record, device_id, expected in cases:
            self.assertEqual(env.call_summary(record, device_id), expected, record)
        self.assertIsNone(env.call_summary({'direction': 'incoming', 'outcome': 'teleported'}))
        self.assertIsNone(env.call_summary(None))

    def test_legacy_rows(self):
        cases = [(str([""" (1'05")""", '', 'audio']), 'incoming', 'completed', 65, 'Incoming call (1:05)'),
                 (str([""" (1h02'05")""", '', 'video']), 'outgoing', 'completed', 3725, 'Outgoing video call (1:02:05)'),
                 (str([0, '', 'audio']), 'incoming', 'missed', 0, 'Missed call'),
                 (str([0, 'Cancelled', 'audio']), 'outgoing', 'cancelled', 0, 'Cancelled call'),
                 (str([0, 'Busy Here', 'audio']), 'outgoing', 'failed', 0, 'Call failed \u2014 Busy Here'),
                 (str([0, '', 'audio']), 'outgoing', 'cancelled', 0, 'Cancelled call')]
        for content, direction, outcome, duration, summary in cases:
            record = env.legacy_call_record(content, direction, 'm1', timestamp='2026-01-02 03:04:05', remote_party='bob@example.com')
            self.assertEqual((record['outcome'], record['duration'], record['source'], record['sessionId']), (outcome, duration, 'migrated', 'm1'), content)
            self.assertEqual(env.call_summary(record), summary, content)
        self.assertEqual(record['startTime'], '2026-01-02T03:04:05+00:00')
        self.assertEqual(record['remoteParty'], 'bob@example.com')

    def test_legacy_rows_are_not_evaluated(self):
        for content in ("__import__('os').system('true')", '[1, 2]', 'garbage', '', None, b"[0, '', 'audio']"):
            record = env.legacy_call_record(content, 'incoming', 'm1')
            self.assertTrue(record is None or record['outcome'] == 'missed', content)
        self.assertIsNone(env.legacy_call_record("__import__('os').system('true')", 'incoming', 'm1'))

    def test_dominant_media(self):
        self.assertEqual(env.dominant_media('audio, video'), 'video')
        self.assertEqual(env.dominant_media(['chat', 'audio']), 'audio')
        self.assertEqual(env.dominant_media(['chat']), 'chat')
        self.assertEqual(env.dominant_media(None), 'audio')

    def test_device_id_without_sipsimple_is_none_or_bare(self):
        device_id = env.this_device_id()
        self.assertTrue(device_id is None or not device_id.lower().startswith('urn:uuid:'))


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
