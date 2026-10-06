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


_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.pstn_normalize', 'blink.contact_repair')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
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


def contact(name, *uris):
    return types.SimpleNamespace(name=name, uris=[types.SimpleNamespace(uri=uri, id='u%d' % index) for index, uri in enumerate(uris)])


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
