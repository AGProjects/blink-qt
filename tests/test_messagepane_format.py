"""Tests for blink.messagepane.format. Run from the top of the tree: python3 -m unittest tests.test_messagepane_format"""

import importlib.util
import os
import unittest


def _load():
    path = os.path.join(os.path.dirname(__file__), os.pardir, 'blink', 'messagepane', 'format.py')
    spec = importlib.util.spec_from_file_location('messagepane_format_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fmt = _load()


class InitialsTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(fmt.initials('Adrian Georgescu'), 'AG')
        self.assertEqual(fmt.initials('henry'), 'H')
        self.assertEqual(fmt.initials('Jean-Luc van Damme'), 'JD')
        self.assertEqual(fmt.initials('Living233 (Home)'), 'LH')

    def test_address_fallback(self):
        self.assertEqual(fmt.initials('', 'sip:alice@example.com'), 'A')
        self.assertEqual(fmt.initials(None, '+31208005169'), '3')
        self.assertEqual(fmt.initials('', ''), '?')


class ColourTests(unittest.TestCase):
    def test_stable(self):
        self.assertEqual(fmt.avatar_colour('ag@sylk.link'), fmt.avatar_colour('AG@sylk.link'))
        self.assertIn(fmt.avatar_colour('x'), fmt.AVATAR_COLOURS)


class SummaryTests(unittest.TestCase):
    def item(self, category, content, content_type='text/plain'):
        import types
        return types.SimpleNamespace(category=category, content=content, content_type=content_type)

    def test_text(self):
        self.assertEqual(fmt.plain_summary(self.item('text', 'hello\n  world')), 'hello world')
        self.assertEqual(fmt.plain_summary(self.item('text', '<b>hi</b> there', 'text/html')), 'hi there')
        self.assertEqual(fmt.plain_summary(self.item('text', '-----BEGIN PGP MESSAGE-----\nx\n-----END PGP MESSAGE-----')), '🔒 Encrypted message')

    def test_other(self):
        self.assertEqual(fmt.plain_summary(self.item('image', '{}')), '🖼 Picture')
        self.assertEqual(fmt.plain_summary(self.item('call', 'Outgoing audio call, 2 min')), '📞 Call: Outgoing audio call, 2 min')


class HTMLTests(unittest.TestCase):
    def test_linkify(self):
        self.assertEqual(fmt.linkify('see https://example.com/a(b). ok'),
                         'see <a href="https://example.com/a(b)">https://example.com/a(b)</a>. ok')
        self.assertEqual(fmt.linkify('www.x.org, me@x.com'), '<a href="https://www.x.org">www.x.org</a>, <a href="mailto:me@x.com">me@x.com</a>')
        self.assertEqual(fmt.linkify('a <b>\nc'), 'a &lt;b&gt;<br>c')

    def test_sanitize(self):
        self.assertEqual(fmt.sanitize_html('<p onclick="x">hi <script>alert(1)</script><a href="javascript:x">j</a> <a href="https://a.b">ok</a><img src=x><b>bold'),
                         '<p>hi <a>j</a> <a href="https://a.b">ok</a><b>bold</b></p>')
        self.assertEqual(fmt.sanitize_html('<style>p{}</style><i>x</i>'), '<i>x</i>')


class KindTests(unittest.TestCase):
    def item(self, category, content, content_type='text/plain'):
        import types
        return types.SimpleNamespace(category=category, content=content, content_type=content_type)

    def test_kinds(self):
        self.assertEqual(fmt.bubble_kind(self.item('text', 'hello')), 'text')
        self.assertEqual(fmt.bubble_kind(self.item('text', '{"a": 1}', 'application/json')), 'summary')
        self.assertEqual(fmt.bubble_kind(self.item('text', 'Public key received')), 'note')
        self.assertEqual(fmt.bubble_kind(self.item('text', '-----BEGIN PGP MESSAGE-----\nx')), 'encrypted')
        self.assertEqual(fmt.bubble_kind(self.item('image', '{}', 'application/vnd.gsma.rcs-ft-http+xml')), 'summary')


class DayTests(unittest.TestCase):
    def test_labels(self):
        from datetime import date
        today = date(2026, 1, 2)
        self.assertEqual(fmt.day_label(date(2026, 1, 2), today), 'Today')
        self.assertEqual(fmt.day_label(date(2026, 1, 1), today), 'Yesterday')        # across the year end
        self.assertEqual(fmt.day_label(date(2025, 12, 29), today), 'Monday')
        self.assertEqual(fmt.day_label(date(2025, 12, 1), today), '1 December 2025')
        self.assertEqual(fmt.day_label(date(2026, 3, 5), date(2026, 3, 20)), '5 March')

    def test_marks(self):
        import types
        self.assertEqual(fmt.delivery_mark(types.SimpleNamespace(direction='outgoing', state='displayed')), ('✔✔', 'displayed'))
        self.assertEqual(fmt.delivery_mark(types.SimpleNamespace(direction='incoming', state='displayed')), ('', None))
        self.assertEqual(fmt.delivery_mark(types.SimpleNamespace(direction='outgoing', state='failed-local')), ('⚠', 'failed'))


if __name__ == '__main__':
    unittest.main()
