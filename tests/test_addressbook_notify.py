"""Tests for blink.addressbook_notify. Run from the top of the tree: python3 -m unittest tests.test_addressbook_notify"""

import importlib.util
import json
import os
import unittest


def _load():
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', 'addressbook_notify.py')
    spec = importlib.util.spec_from_file_location('addressbook_notify_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notify = _load()


class Clock(object):
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TickTests(unittest.TestCase):
    def test_round_trip(self):
        body = notify.build_tick('dev-1', ['c2', 'c1', 'c1', None], ['g1'], timestamp=1234)
        tick = notify.parse_tick(body)
        self.assertEqual(tick['origin'], 'dev-1')
        self.assertEqual(tick['timestamp'], 1234)
        self.assertEqual(tick['contact_ids'], ['c2', 'c1'])
        self.assertEqual(tick['group_ids'], ['g1'])
        self.assertFalse(tick['truncated'])

    def test_truncated_omits_both_lists(self):
        body = json.loads(notify.build_tick('dev-1', ['c1'], ['g1'], truncated=True, timestamp=1))
        self.assertTrue(body['truncated'])
        self.assertNotIn('contactIds', body)
        self.assertNotIn('groupIds', body)
        tick = notify.parse_tick(json.dumps(body))
        self.assertIsNone(tick['contact_ids'])      # absent means "assume everything changed"
        self.assertIsNone(tick['group_ids'])

    def test_empty_lists_stay_empty(self):
        tick = notify.parse_tick(notify.build_tick('d', timestamp=1))
        self.assertEqual(tick['contact_ids'], [])
        self.assertEqual(tick['group_ids'], [])

    def test_rejects_anything_else(self):
        for content in (b'\xff', 'not json', '[]', '{"v": 2, "timestamp": 1}', '{"v": 1, "timestamp": "1"}', None):
            self.assertIsNone(notify.parse_tick(content), content)
        self.assertIsNotNone(notify.parse_tick(b'{"v": 1, "timestamp": 1}'))

    def test_freshness(self):
        self.assertTrue(notify.is_fresh(1000, now=1000))
        self.assertTrue(notify.is_fresh(1000, now=1120))
        self.assertFalse(notify.is_fresh(1000, now=1121))
        self.assertTrue(notify.is_fresh(1010, now=1000))    # ten seconds of skew
        self.assertFalse(notify.is_fresh(1011, now=1000))
        self.assertFalse(notify.is_fresh('1000', now=1000))

    def test_jitter_range(self):
        self.assertEqual(notify.jitter_delay(lambda: 0.0), notify.FETCH_JITTER_MIN)
        self.assertEqual(notify.jitter_delay(lambda: 1.0), notify.FETCH_JITTER_MAX)


class SendThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.throttle = notify.SendThrottle(now=self.clock)

    def test_idle(self):
        self.assertFalse(self.throttle.pending)
        self.assertIsNone(self.throttle.delay())
        self.assertIsNone(self.throttle.take())

    def test_burst_is_one_tick_after_debounce(self):
        self.throttle.note('contact', 'c1')
        self.clock.advance(1)
        self.throttle.note('contact', 'c2')
        self.throttle.note('group', 'g1')
        self.assertIsNone(self.throttle.take())
        self.assertAlmostEqual(self.throttle.delay(), notify.NOTIFY_DEBOUNCE)
        self.clock.advance(notify.NOTIFY_DEBOUNCE)
        self.assertEqual(self.throttle.take(), (['c1', 'c2'], ['g1'], False))
        self.assertFalse(self.throttle.pending)

    def test_not_settled_holds_the_tick(self):
        self.throttle.note('contact', 'c1')
        self.clock.advance(notify.NOTIFY_DEBOUNCE)
        self.assertIsNone(self.throttle.take(settled=False))
        self.assertTrue(self.throttle.pending)
        self.assertEqual(self.throttle.take(settled=True), (['c1'], [], False))

    def test_max_defer_flushes_a_drip_truncated(self):
        for _ in range(int(notify.NOTIFY_MAX_DEFER)):
            self.throttle.note('contact', 'c1')
            self.clock.advance(1)
        self.throttle.note('contact', 'c2')
        self.assertEqual(self.throttle.delay(), 0)
        contacts, groups, truncated = self.throttle.take()
        self.assertTrue(truncated)                   # still being written to

    def test_min_interval(self):
        self.throttle.note('contact', 'c1')
        self.clock.advance(notify.NOTIFY_DEBOUNCE)
        self.assertIsNotNone(self.throttle.take())
        self.throttle.note('contact', 'c2')
        self.clock.advance(notify.NOTIFY_DEBOUNCE)
        self.assertIsNone(self.throttle.take())
        self.assertAlmostEqual(self.throttle.delay(), notify.NOTIFY_MIN_INTERVAL - notify.NOTIFY_DEBOUNCE)

    def test_unnamed_write_truncates(self):
        self.throttle.note('contact', None)
        self.clock.advance(notify.NOTIFY_DEBOUNCE)
        self.assertTrue(self.throttle.take()[2])

    def test_id_cap_truncates(self):
        for index in range(notify.ID_LIST_CAP + 1):
            self.throttle.note('contact', 'c%d' % index)
        self.clock.advance(notify.NOTIFY_DEBOUNCE)
        self.assertTrue(self.throttle.take()[2])

    def test_suppressed_is_reentrant(self):
        self.throttle.suppress()
        self.throttle.suppress()
        self.throttle.resume()
        self.assertFalse(self.throttle.note('contact', 'c1'))
        self.throttle.resume()
        self.assertTrue(self.throttle.note('contact', 'c1'))

    def test_fuse(self):
        for index in range(notify.NOTIFY_FUSE_MAX):
            self.throttle.note('contact', 'c%d' % index)
            self.clock.advance(notify.NOTIFY_MIN_INTERVAL)
            self.assertIsNotNone(self.throttle.take())
        self.assertTrue(self.throttle.fuse_blown)
        self.throttle.note('contact', 'late')
        self.clock.advance(notify.NOTIFY_MIN_INTERVAL)
        self.assertIsNone(self.throttle.take())
        self.assertTrue(self.throttle.pending)        # kept, only late
        self.clock.advance(notify.NOTIFY_FUSE_WINDOW)
        self.assertEqual(self.throttle.take()[0], ['late'])


class FetchSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.scheduler = notify.FetchScheduler(now=self.clock, rand=lambda: 0.0)

    def test_jittered_then_merged(self):
        self.assertEqual(self.scheduler.schedule(), notify.FETCH_JITTER_MIN)
        self.assertIsNone(self.scheduler.schedule())  # merged into the armed fetch
        self.assertEqual(self.scheduler.fire(), (True, None, False))

    def test_tick_during_flight_refetches_after(self):
        self.scheduler.schedule()
        self.scheduler.fire()
        self.assertIsNone(self.scheduler.schedule())
        self.assertEqual(self.scheduler.done(), notify.FETCH_MIN_INTERVAL)
        self.assertIsNone(self.scheduler.done())

    def test_not_settled_reschedules(self):
        self.scheduler.schedule()
        fetch, retry_in, backed_off = self.scheduler.fire(settled=False)
        self.assertFalse(fetch)
        self.assertEqual(retry_in, notify.FETCH_JITTER_MIN)
        self.assertTrue(self.scheduler.armed)

    def test_fuse_backs_off(self):
        for _ in range(notify.FETCH_FUSE_MAX):
            self.scheduler.schedule()
            self.assertTrue(self.scheduler.fire()[0])
            self.scheduler.done()
            self.clock.advance(notify.FETCH_MIN_INTERVAL)
        self.scheduler.schedule()
        fetch, retry_in, backed_off = self.scheduler.fire()
        self.assertFalse(fetch)
        self.assertTrue(backed_off)
        self.assertEqual(retry_in, notify.FETCH_BACKOFF[0])


if __name__ == '__main__':
    unittest.main()
