"""Tests for blink.contact_repair. Run from the top of the tree: python3 -m unittest tests.test_contact_repair"""

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


_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.pstn_normalize', 'blink.contact_repair', 'blink.conference')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
# blink.conference reads the accounts (sipsimple): a bridge at the sip2sip conference server stands in
_conference = types.ModuleType('blink.conference')
_conference.is_conference_uri = lambda uri, account=None: str(uri or '').lower().rpartition('@')[2] in ('conference.sip2sip.info', 'videoconference.sip2sip.info')
sys.modules['blink.conference'] = _conference
_load('blink.pstn_normalize', 'pstn_normalize.py')
repair = _load('blink.contact_repair', 'contact_repair.py')


def tearDownModule():
    for name, module in _saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class _PSTN(object):
    idd_prefix = None
    prefix = None
    replace_leading_zero = '0031'


class _Account(object):
    pstn = _PSTN()


NL = _Account()


def contact(name, *uris, types_=()):
    kinds = list(types_) + [None] * (len(uris) - len(types_))
    return types.SimpleNamespace(name=name, uris=[types.SimpleNamespace(uri=uri, id='u%d' % index, type=kind) for index, (uri, kind) in enumerate(zip(uris, kinds))])


class ConferenceTests(unittest.TestCase):
    def test_server_domain(self):
        self.assertEqual(repair.server_conference_uri('338318@videoconference.sip2sip.info'), '338318@conference.sip2sip.info')
        self.assertEqual(repair.server_conference_uri('sip:338318@VideoConference.sip2sip.info'), '338318@conference.sip2sip.info')
        for uri in ('338318@conference.sip2sip.info', 'alice@example.com', '+31201234567', '', None):
            self.assertEqual(repair.server_conference_uri(uri), uri)


class NameTests(unittest.TestCase):
    def test_echoed_names(self):
        self.assertEqual(repair.echoed_name_replacement(contact('+31618853125@sylk.link', '+31618853125')), '+31618853125')
        self.assertEqual(repair.echoed_name_replacement(contact('9495088@conference.sip2sip.info', '9495088@conference.sip2sip.info')), '9495088')
        self.assertEqual(repair.echoed_name_replacement(contact('338318@videoconference.sip2sip.info', '338318@conference.sip2sip.info')), '338318')
        self.assertEqual(repair.echoed_name_replacement(contact('0034913336701', '+34913336701')), '+34913336701')

    def test_real_names_kept(self):
        for item in (contact('Nissan Rustman', '+31618853125'), contact('Mama', 'mama@example.com'),
                     contact('1233', '1233@sylk.link'), contact('9495088', '9495088@conference.sip2sip.info'),
                     contact('', 'a@b'), contact('Alice')):
            self.assertIsNone(repair.echoed_name_replacement(item), item.name)


class PlanTests(unittest.TestCase):
    def test_full_plan(self):
        item = contact('+31646630425@sylk.link', '+31646630425@sylk.link', '0646630425', '338318@videoconference.sip2sip.info')
        plan = repair.repair_plan(item, NL)
        self.assertEqual([(current, wanted, reason) for uri, current, wanted, reason in plan['addresses']],
                         [('+31646630425@sylk.link', '+31646630425', 'e164'), ('0646630425', '+31646630425', 'e164'),
                          ('338318@videoconference.sip2sip.info', '338318@conference.sip2sip.info', 'conference-domain')])
        self.assertEqual(plan['name'], ('+31646630425@sylk.link', '+31646630425'))
        self.assertEqual([(dropped.id, kept.id) for dropped, kept in plan['duplicates']], [('u1', 'u0')])
        self.assertEqual(item.uris[0].uri, '+31646630425@sylk.link')      # nothing changed by planning

    def test_nothing_to_do(self):
        self.assertEqual(repair.repair_plan(contact('Alice', 'alice@example.com', '+31201234567'), NL), {})

    def test_duplicate_spelling_case(self):
        plan = repair.repair_plan(contact('Echo', 'echo@conference.sip2sip.info', 'Echo@conference.sip2sip.info'), NL)
        self.assertEqual(len(plan['duplicates']), 1)


class LowercaseTests(unittest.TestCase):
    def test_addresses_and_types(self):
        plan = repair.repair_plan(contact('Joe', 'Joe@Sylk.Link', '+31201234567', 'https://Example.com/Joe', types_=('SIP', 'Tel', 'URL')), NL)
        self.assertEqual([(current, wanted, reason) for uri, current, wanted, reason in plan['addresses']],
                         [('Joe@Sylk.Link', 'joe@sylk.link', 'lowercase')])
        self.assertEqual([(current, wanted) for uri, current, wanted in plan['types']], [('SIP', 'sip'), ('Tel', 'tel'), ('URL', 'url')])

    def test_already_lowercase(self):
        self.assertEqual(repair.repair_plan(contact('Joe', 'joe@sylk.link', types_=('sip',)), NL), {})


class MembershipTests(unittest.TestCase):
    def group(self, id, *members):
        return types.SimpleNamespace(id=id, name=id, contacts=[types.SimpleNamespace(id=member) for member in members])

    def setUp(self):
        self.survivor, self.loser = types.SimpleNamespace(id='a'), types.SimpleNamespace(id='b')

    def plan(self, *groups, room=False):
        result = repair.membership_plan(self.survivor, [self.loser], list(groups), '_deleted', '_messages', '_blocked', room)
        return {key: [group.id for group in value] for key, value in result.items()}

    def test_losers_groups_are_joined(self):
        self.assertEqual(self.plan(self.group('work', 'b'), self.group('family', 'a', 'b'), self.group('other', 'c')),
                         {'join': ['work'], 'leave': [], 'ask': []})

    def test_deleted(self):
        self.assertEqual(self.plan(self.group('_deleted', 'b')), {'join': [], 'leave': [], 'ask': []})           # only the copy was deleted
        self.assertEqual(self.plan(self.group('_deleted', 'a')), {'join': [], 'leave': ['_deleted'], 'ask': []})  # the copy was not
        self.assertEqual(self.plan(self.group('_deleted', 'a', 'b')), {'join': [], 'leave': [], 'ask': []})       # both were

    def test_room_not_in_messages(self):
        self.assertEqual(self.plan(self.group('_messages', 'b'), room=True), {'join': [], 'leave': [], 'ask': []})
        self.assertEqual(self.plan(self.group('_messages', 'b')), {'join': ['_messages'], 'leave': [], 'ask': []})

    def test_blocked_is_asked(self):
        self.assertEqual(self.plan(self.group('_blocked', 'b')), {'join': [], 'leave': [], 'ask': ['_blocked']})
        self.assertEqual(self.plan(self.group('_blocked', 'a', 'b')), {'join': [], 'leave': [], 'ask': []})


class MergeTests(unittest.TestCase):
    key = staticmethod(lambda uri: str(uri).strip().lower().split('@')[0] if str(uri).startswith('+') else str(uri).strip().lower())

    def make(self, id, name, *uris, modified_at=''):
        item = contact(name, *uris)
        item.id, item.modified_at = id, modified_at
        return item

    def test_clusters_and_survivor(self):
        a = self.make('id9', 'Leo', 'leo@sylk.link', modified_at='2026-09-30T10:00:00Z')
        b = self.make('id1', 'leo@sylk.link', 'leo@sylk.link', '+31612345678')
        c = self.make('id5', '+31612345678', '+31612345678@sylk.link', 'leo@work.example')
        d = self.make('id3', 'Alone', 'alone@example.com')
        plan = repair.merge_plan([a, b, c, d], self.key)
        self.assertEqual(len(plan), 1)
        cluster = plan[0]
        self.assertIs(cluster['survivor'], b)                           # lowest id, not the named one
        self.assertEqual([loser.id for loser in cluster['losers']], ['id5', 'id9'])
        self.assertIs(cluster['name'], a)                               # the given name moves over
        self.assertEqual([uri.uri for uri, donor in cluster['uris']], ['leo@work.example'])

    def test_named_survivor_keeps_name(self):
        a = self.make('id1', 'Leo', 'leo@sylk.link')
        b = self.make('id2', 'Leopold', 'LEO@sylk.link')
        plan = repair.merge_plan([a, b], self.key)
        self.assertIsNone(plan[0]['name'])
        self.assertEqual(plan[0]['uris'], [])

    def test_nothing_to_merge(self):
        self.assertEqual(repair.merge_plan([self.make('id1', 'A', 'a@x'), self.make('id2', 'B', 'b@x'), self.make('id3', 'C')], self.key), [])


if __name__ == '__main__':
    unittest.main()
