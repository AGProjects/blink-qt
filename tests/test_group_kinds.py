"""Tests for blink.group_kinds. Run from the top of the tree: python3 -m unittest tests.test_group_kinds"""

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


_saved = {name: sys.modules.get(name) for name in ('blink', 'blink.group_kinds')}
_package = types.ModuleType('blink')
_package.__path__ = []
sys.modules['blink'] = _package
kinds = _load('blink.group_kinds', 'group_kinds.py')


def tearDownModule():
    for name, module in _saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class Group(object):
    def __init__(self, id, name, kind=''):
        self.id, self.name, self.kind = id, name, kind

    def __repr__(self):
        return f'Group({self.id!r}, {self.name!r}, {self.kind!r})'


class FindTests(unittest.TestCase):
    def test_order_of_authority(self):
        renamed = Group('id1', 'Phone log', 'calls')
        by_name = Group('id2', ' calls ')
        reserved = Group('_calls', 'Something')
        self.assertIs(kinds.find_group([by_name, reserved, renamed], kinds.CALLS), renamed)   # kind wins
        self.assertIs(kinds.find_group([reserved, by_name], kinds.CALLS), by_name)            # then name
        self.assertIs(kinds.find_group([reserved], kinds.CALLS), reserved)                    # then id
        self.assertIsNone(kinds.find_group([Group('x', 'Friends')], kinds.CALLS))

    def test_favorites_legacy_id(self):
        legacy = Group('favorites', 'Starred')
        self.assertIs(kinds.find_group([legacy], kinds.FAVORITES), legacy)
        self.assertTrue(kinds.is_group(legacy, kinds.FAVORITES))
        self.assertFalse(kinds.is_group(Group('g', 'Friends'), kinds.FAVORITES))
        self.assertFalse(kinds.is_group(None, kinds.FAVORITES))

    def test_kind_case(self):
        self.assertTrue(kinds.is_group(Group('g', 'X', ' TEL '), kinds.TEL))


class StampPlanTests(unittest.TestCase):
    def test_plan(self):
        calls = Group('id1', 'Calls')
        tel = Group('id2', 'Tel', 'tel')
        blocked = Group('id3', 'Blocked', 'muted')          # another client's kind
        plan = {identity.kind: (group, action) for identity, group, action in kinds.stamp_plan([calls, tel, blocked])}
        self.assertEqual(plan['calls'], (calls, 'stamp'))
        self.assertEqual(plan['tel'], (tel, 'stamped'))
        self.assertEqual(plan['blocked'], (blocked, 'foreign'))
        self.assertEqual(plan['conference'], (None, 'missing'))
        self.assertEqual(plan['favorites'], (None, 'missing'))


class DuplicatePlanTests(unittest.TestCase):
    def test_reserved_id_kept(self):
        server = Group('_conference', 'Conference', 'conference')
        early = Group('id1791394378737117890447', 'Conference', 'conference')
        plan = kinds.duplicate_plan([early, server])
        self.assertEqual(len(plan), 1)
        identity, kept, duplicates = plan[0]
        self.assertIs(identity, kinds.CONFERENCE)
        self.assertIs(kept, server)
        self.assertEqual(duplicates, [early])

    def test_most_members_kept_without_reserved_id(self):
        small, big = Group('a', 'Tel', 'tel'), Group('b', 'Tel', 'tel')
        small.contacts, big.contacts = [1], [1, 2, 3]
        identity, kept, duplicates = kinds.duplicate_plan([small, big])[0]
        self.assertIs(kept, big)
        self.assertEqual(duplicates, [small])

    def test_single_groups_untouched(self):
        self.assertEqual(kinds.duplicate_plan([Group('_calls', 'Calls', 'calls'), Group('x', 'Calls')]), [])


if __name__ == '__main__':
    unittest.main()
