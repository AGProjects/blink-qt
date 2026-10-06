"""Tests for blink.location (location sharing payloads v1, v2 and legacy).

Run from the top of the tree: python3 -m unittest tests.test_location
The modules are loaded from their files so the tests need neither Qt nor
sipsimple. Decryption is simulated: the "armour" wraps the plaintext.
"""

import datetime
import importlib.util
import json
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


# blink/__init__.py pulls in Qt; give message_envelopes a bare package to find blink.location in
_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.location', 'blink.message_envelopes')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
location = _load('blink.location', 'location.py')
envelopes = _load('blink.message_envelopes', 'message_envelopes.py')


def tearDownModule():
    for name, module in _saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


HEADER, FOOTER = '-----BEGIN PGP MESSAGE-----', '-----END PGP MESSAGE-----'


def armour(value):
    return '%s\n%s\n%s' % (HEADER, json.dumps(value) if not isinstance(value, str) else value, FOOTER)


def decrypt(text):
    return text.strip()[len(HEADER):-len(FOOTER)].strip()


POSITION = {'latitude': 52.3702, 'longitude': 4.8952, 'accuracy': 12, 'timestamp': '2026-10-06T10:00:00Z'}


def v1(action, value=None, **kw):
    envelope = {'action': action}
    if value is not None:
        envelope['value'] = value
    envelope.update(kw)
    return json.dumps(envelope)


def v2(action, value=None, **kw):
    metadata = {'action': action, 'version': '2.0'}
    metadata.update(kw)
    return (armour(value) if value is not None else ''), json.dumps(metadata)


class EnvelopeTests(unittest.TestCase):
    def test_versions(self):
        for raw, expected in (('2', 2), ('2.0', 2), ('2.1.3', 2), (2, 2), (2.0, 2), ('', None), (None, None), (True, None), ('x', None)):
            self.assertEqual(location.envelope_version({'version': raw}), expected, raw)

    def test_v2_splices_coordinates_after_action(self):
        content, metadata = v2('location_start', POSITION, sessionId='s1', deviceId='d1')
        envelope = location.location_envelope(content, metadata)
        self.assertEqual(list(envelope)[:2], ['action', 'value'])
        self.assertEqual(envelope['sessionId'], 's1')

    def test_v2_metadata_cannot_forge_value(self):
        content, metadata = v2('location_start', POSITION, sessionId='s1')
        forged = json.loads(metadata)
        forged['value'] = {'latitude': 0, 'longitude': 0}
        envelope = location.location_envelope(content, json.dumps(forged))
        self.assertTrue(envelope['value'].startswith(HEADER))

    def test_v1_mirror_metadata_is_ignored(self):
        body = v1('location_start', armour(POSITION), sessionId='s1')
        envelope = location.location_envelope(body, json.dumps({'action': 'location_start', 'version': '1.0'}))
        self.assertEqual(envelope['sessionId'], 's1')

    def test_unparseable(self):
        self.assertIsNone(location.location_envelope('not json'))
        self.assertIsNone(location.location_envelope(''))
        self.assertIsNone(location.location_envelope(armour({'action': 'location'})))   # legacy armoured, no key


class PayloadTests(unittest.TestCase):
    def test_v1_coordinate_tick(self):
        payload = location.location_payload(v1('location_start', armour(POSITION), sessionId='s1'), decrypt=decrypt,
                                            content_type=location.LOCATION_CONTENT_TYPE)
        self.assertEqual(payload['action'], 'location_start')
        self.assertEqual(payload['session_id'], 's1')
        self.assertEqual((payload['coords']['latitude'], payload['coords']['longitude'], payload['coords']['accuracy']), (52.3702, 4.8952, 12.0))
        self.assertTrue(payload['coords']['maps_url'].startswith('https://www.openstreetmap.org/?mlat=52.3702000&mlon=4.8952000'))
        self.assertFalse(payload['legacy'])

    def test_v2_coordinate_tick(self):
        content, metadata = v2('location_update', POSITION, sessionId='s1')
        payload = location.location_payload(content, metadata, decrypt=decrypt, content_type=location.LOCATION_CONTENT_TYPE)
        self.assertTrue(payload['is_update'])
        self.assertEqual(payload['version'], 2)

    def test_coordinate_tick_without_key_is_not_renderable(self):
        content, metadata = v2('location_start', POSITION, sessionId='s1')
        self.assertIsNone(location.location_payload(content, metadata))

    def test_v2_signal_with_empty_content(self):
        content, metadata = v2('location_stop', sessionId='s1', reason='returned')
        payload = location.location_payload(content, metadata)
        self.assertTrue(payload['is_signal'])
        self.assertIsNone(payload['coords'])
        self.assertEqual(location.system_note(payload, 'Alice', 'incoming'), '\U0001F4CD Alice returned')
        self.assertEqual(location.ended_label(payload), 'Returned')

    def test_meeting_destination(self):
        wrapped = {'value': POSITION, 'destination': {'latitude': 52.0, 'longitude': 4.0}}
        payload = location.location_payload(v1('meeting_request', armour(wrapped), sessionId='m1', role='inviter'), decrypt=decrypt)
        self.assertEqual(payload['coords']['destination'], {'latitude': 52.0, 'longitude': 4.0})
        self.assertEqual(location.bubble_id(payload), 'm1:inviter')
        self.assertEqual(location.session_bubble_ids({'session_id': 'm1'}), ['m1', 'm1:inviter', 'm1:invited'])

    def test_legacy_metadata_format(self):
        origin = armour({'action': 'location', 'messageId': 'L1', 'metadataId': None, 'value': POSITION})
        payload = location.location_payload(origin, decrypt=decrypt, content_type=location.LEGACY_LOCATION_CONTENT_TYPE)
        self.assertEqual((payload['action'], payload['session_id'], payload['legacy']), ('location_start', 'L1', True))
        tick = armour({'action': 'location', 'messageId': 'L1', 'metadataId': 'x', 'value': POSITION})
        self.assertTrue(location.location_payload(tick, decrypt=decrypt, content_type=location.LEGACY_LOCATION_CONTENT_TYPE)['is_update'])

    def test_session_precedence(self):
        self.assertEqual(location.envelope_session_and_source({'sessionId': 's', 'messageId': 'm'}), ('s', 'sessionId'))
        self.assertEqual(location.envelope_session_and_source({'messageId': 'm'}), ('m', 'messageId'))
        self.assertEqual(location.envelope_session_and_source({}), (None, 'none'))

    def test_other_metadata_flavours(self):
        reply = envelopes.reply_envelope('r', 'o', 'm', 'u', 1)
        self.assertIsNone(location.location_payload(reply, content_type=location.LEGACY_LOCATION_CONTENT_TYPE))


class SummaryAndCategoryTests(unittest.TestCase):
    def test_summary_needs_no_key(self):
        content, metadata = v2('location_start', POSITION, sessionId='s1', deviceId='d1')
        summary = location.envelope_summary(content, metadata, location.LOCATION_CONTENT_TYPE)
        self.assertEqual((summary['action'], summary['session_id'], summary['category'], summary['is_notable']), ('location_start', 's1', 'location', True))
        self.assertEqual(summary['store_metadata'], {'version': '2.0', 'deviceId': 'd1'})

    def test_categories(self):
        cases = [(v2('location_start', POSITION, sessionId='s1'), 'location'),
                 (v2('location_once', POSITION, messageId='o1'), 'location'),
                 (v2('meeting_request', POSITION, sessionId='m1'), 'location'),
                 (v2('location_update', POSITION, sessionId='s1'), None),
                 (v2('meeting_update', POSITION, sessionId='m1'), None),
                 (v2('location_stop', sessionId='s1'), None),
                 (v2('location_request', messageId='q1'), None),
                 ((v1('location_start', armour(POSITION), sessionId='s1'), None), 'location'),
                 ((v1('location_update', armour(POSITION), sessionId='s1'), None), None),
                 (('garbage', None), None)]
        for (content, metadata), expected in cases:
            self.assertEqual(envelopes.classify_category(location.LOCATION_CONTENT_TYPE, content, metadata=metadata), expected, (content, metadata))

    def test_row_metadata_round_trip(self):
        content, metadata = v2('location_start', POSITION, sessionId='s1', deviceId='d1')
        summary = location.envelope_summary(content, metadata, location.LOCATION_CONTENT_TYPE)
        rebuilt = location.row_metadata(json.dumps(summary['store_metadata']), 'location_start', 's1')
        payload = location.location_payload(content, rebuilt, decrypt=decrypt, content_type=location.LOCATION_CONTENT_TYPE)
        self.assertEqual((payload['action'], payload['session_id'], payload['device_id']), ('location_start', 's1', 'd1'))
        self.assertIsNone(location.row_metadata(None, 'location_start', 's1'))       # a v1 row needs no side-band
        self.assertEqual(location.row_metadata('{"x": 1}', None), '{"x": 1}')         # not a location row


class TrackTests(unittest.TestCase):
    def point(self, lat, minute):
        return {'latitude': lat, 'longitude': 4.0, 'timestamp': '2026-10-06T10:%02d:00Z' % minute}

    def test_ordered_insert_and_dedup(self):
        track = []
        location.append_track_point(track, self.point(52.0, 1))
        location.append_track_point(track, self.point(52.2, 3))
        location.append_track_point(track, self.point(52.1, 2))           # history catching up
        location.append_track_point(track, self.point(52.2, 4))           # same spot, newer
        self.assertEqual([p['latitude'] for p in track], [52.0, 52.1, 52.2])
        self.assertEqual(track[-1]['timestamp'], '2026-10-06T10:04:00Z')

    def test_cap(self):
        track = []
        for index in range(location.MAX_TRACK_POINTS + 5):
            location.append_track_point(track, {'latitude': index * 0.001, 'longitude': 4.0})
        self.assertEqual(len(track), location.MAX_TRACK_POINTS)
        self.assertAlmostEqual(track[0]['latitude'], 0.005)

    def test_merge_keeps_trail(self):
        start = location.storable_envelope({'envelope': {'action': 'location_start', 'sessionId': 's1'}, 'coords': self.point(52.0, 1), 'session_id': 's1'})
        tick = location.storable_envelope({'envelope': {'action': 'location_update', 'sessionId': 's1'}, 'coords': self.point(52.1, 2), 'session_id': 's1'})
        merged = location.merge_location_bodies(start, tick)
        again = location.merge_location_bodies(merged, location.storable_envelope({'envelope': {'action': 'location_update', 'sessionId': 's1'}, 'coords': self.point(52.2, 3), 'session_id': 's1'}))
        envelope = json.loads(again)
        self.assertEqual([p['latitude'] for p in envelope['track']], [52.0, 52.1, 52.2])
        self.assertEqual(envelope['value']['latitude'], 52.2)
        # a single-position writer cannot flatten the trail
        stale = location.merge_location_bodies(again, tick)
        self.assertEqual(len(json.loads(stale)['track']), 3)

    def test_stored_row_reads_back_without_key(self):
        body = location.storable_envelope({'envelope': {'action': 'location_once', 'messageId': 'o1'}, 'coords': dict(POSITION), 'session_id': 'o1'})
        payload = location.location_payload(body, content_type=location.LOCATION_CONTENT_TYPE)
        self.assertEqual((payload['session_id'], payload['coords']['latitude']), ('o1', 52.3702))


class SenderTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 6, 10, 0, tzinfo=datetime.timezone.utc)

    def test_one_shot(self):
        body = location.one_shot_envelope(POSITION, 'o1', now=self.NOW)
        envelope = json.loads(body)
        self.assertEqual((envelope['action'], envelope['version'], envelope['expires']), ('location_once', '1.0', '2026-10-07T10:00:00+00:00'))
        payload = location.location_payload(body, content_type=location.LOCATION_CONTENT_TYPE)
        self.assertTrue(payload['one_shot'])
        self.assertTrue(location.is_notable_action(payload))
        self.assertIsNone(location.one_shot_envelope({'latitude': None}, 'o2'))

    def test_request(self):
        body = location.location_request_envelope('q1', now=self.NOW)
        payload = location.location_payload(body, content_type=location.LOCATION_CONTENT_TYPE)
        self.assertEqual((payload['action'], payload['session_id'], payload['is_signal']), ('location_request', 'q1', True))
        self.assertEqual(location.system_note(payload, 'Bob', 'outgoing'), '\U0001F4CD Location requested')


if __name__ == '__main__':
    unittest.main()
