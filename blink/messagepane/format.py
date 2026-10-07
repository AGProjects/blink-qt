"""Pure helpers of the message pane: no Qt, no SIP, tested in tests/test_messagepane_format.py."""

import hashlib
import html
import re

from html.parser import HTMLParser


__all__ = ['initials', 'avatar_colour', 'AVATAR_COLOURS', 'plain_summary', 'linkify', 'sanitize_html', 'bubble_kind', 'is_system_note',
           'TEXT_CONTENT_TYPES', 'day_label', 'delivery_mark', 'auto_fetch_reason', 'format_size']


# Backgrounds for initials, white text on each reads in light and dark themes.
AVATAR_COLOURS = ('#e57373', '#f06292', '#ba68c8', '#9575cd', '#7986cb', '#64b5f6', '#4fc3f7', '#4db6ac',
                  '#81c784', '#aed581', '#ffb74d', '#ff8a65', '#a1887f', '#90a4ae')

_SCHEME = re.compile(r'^(sips?|tel):', re.IGNORECASE)


def initials(name, uri=''):
    """Up to two letters standing for a contact: the first letters of the first and last words
    of the name; with no usable name, the first character of the address's user part."""
    words = [word for word in re.split(r'[\s\-_.()]+', str(name or '')) if word and word[0].isalnum()]
    if words:
        letters = words[0][0] + (words[-1][0] if len(words) > 1 else '')
        return letters.upper()
    user = _SCHEME.sub('', str(uri or '')).split('@')[0].lstrip('+')
    return user[:1].upper() if user else '?'


def avatar_colour(key):
    """The background of a contact's initials: the same for the same conversation key everywhere."""
    digest = hashlib.sha1(str(key or '').lower().encode('utf-8')).digest()
    return AVATAR_COLOURS[digest[0] % len(AVATAR_COLOURS)]


# A file's clip and a location's pin are drawn (BubbleDelegate.summary_icons): 📎 and 📍 are missing from common fonts
_CATEGORY_LABELS = {'audio': '🎤 Audio', 'image': '🖼 Picture', 'video': '🎬 Video', 'location': 'Location',
                    'call': '📞 Call', 'other': 'File'}

_FILE_NAME_RES = (re.compile(r'"filename"\s*:\s*"((?:[^"\\]|\\.)*)"'), re.compile(r'<file-name>\s*([^<]*?)\s*</file-name>'))


def file_name(content):
    """The file name a file transfer envelope (Sylk JSON or RCS XML) carries, or ''."""
    for pattern in _FILE_NAME_RES:
        match = pattern.search(content or '')
        if match:
            name = match.group(1).replace('\\"', '"').replace('\\\\', '\\')
            name = name.replace('\\', '/').rsplit('/', 1)[-1]
            return name[:-4] if name.lower().endswith('.asc') else name
    return ''


def plain_summary(item):
    """One line for a row until it has a bubble of its own: the text, or what kind of message it is."""
    content = item.content if isinstance(item.content, str) else (item.content or b'').decode('utf-8', 'replace')
    if item.category == 'text':
        if '-----BEGIN PGP MESSAGE-----' in content:
            return '🔒 Encrypted message'
        if item.content_type == 'text/html':
            content = re.sub(r'<[^>]+>', '', content)
        return ' '.join(content.split())
    label = _CATEGORY_LABELS.get(item.category, item.category or '?')
    if item.category == 'call':
        return f'{label}: {" ".join(content.split())}' if content.strip() else label
    if item.category in ('image', 'audio', 'video', 'other'):
        name = file_name(content)
        if name:
            return f'{label}: {name}'
    return label


# Text bodies drawn as text; anything else in the text category is summarised, never drawn raw.
TEXT_CONTENT_TYPES = ('text/plain', 'text/html', 'text')

# Texts a client writes on its own, drawn as a centred note rather than a bubble
# (the same texts blink.message_envelopes.conversation_preview passes over).
_NOTE_RE = re.compile('^(?:'
                      'Meeting (?:request|expired|cancelled|stopped|succeeded)\\b'
                      '|\U0001F4CD '
                      '|You met\\b'
                      '|I want to meet (?:up with you|with you, too!?)'
                      '|I am sharing the location with you'
                      '|Could you share your current location'
                      ')')
_ARRIVAL_RE = re.compile('arrived at the meeting point\\s*$', re.I)


def is_system_note(text):
    stripped = (text or '').strip()
    return bool(' call ended ' in stripped or stripped.startswith('Public key received') or 'Public key received' in stripped[:40]
                or _NOTE_RE.match(stripped) or _ARRIVAL_RE.search(stripped))


def bubble_kind(item):
    """How a row is drawn: 'text' (a bubble with the body), 'encrypted' (a bubble saying so),
    'note' (a centred line), or 'summary' (a bubble with what kind of message it is)."""
    if item.category != 'text':
        return 'summary'
    content = item.content if isinstance(item.content, str) else (item.content or b'').decode('utf-8', 'replace')
    if '-----BEGIN PGP MESSAGE-----' in content:
        return 'encrypted'
    if str(item.content_type or '').lower() not in TEXT_CONTENT_TYPES:
        return 'summary'
    text = re.sub(r'<[^>]+>', '', content) if item.content_type == 'text/html' else content
    return 'note' if is_system_note(text) else 'text'


_URL_RE = re.compile(r'(?i)\b((?:https?://|www\.)[^\s<>"\']+|mailto:[^\s<>"\']+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)')
_TRAILING = '.,;:!?)]}\''


def linkify(text):
    """Plain text as HTML: escaped, line breaks kept, web and mail addresses as links."""
    parts, last = [], 0
    for match in _URL_RE.finditer(text or ''):
        url = match.group(1)
        stripped = url.rstrip(_TRAILING)
        if url[len(stripped):].startswith(')') and stripped.count('(') > stripped.count(')'):
            stripped += ')'         # a link to a page with brackets in its name
        start, end = match.start(1), match.start(1) + len(stripped)
        parts.append(html.escape(text[last:start]))
        if stripped.lower().startswith(('http://', 'https://', 'mailto:')):
            href = stripped
        elif stripped.lower().startswith('www.'):
            href = 'https://' + stripped
        else:
            href = 'mailto:' + stripped
        parts.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(stripped)}</a>')
        last = end
    parts.append(html.escape((text or '')[last:]))
    return ''.join(parts).replace('\r\n', '\n').replace('\n', '<br>')


_ALLOWED_TAGS = {'a', 'b', 'strong', 'i', 'em', 'u', 's', 'strike', 'del', 'br', 'p', 'div', 'span', 'code', 'pre',
                 'blockquote', 'ul', 'ol', 'li', 'sub', 'sup', 'small', 'big', 'tt'}
_VOID_TAGS = {'br'}
_DROPPED_WITH_CONTENT = {'script', 'style', 'head', 'title', 'iframe', 'object', 'embed', 'svg', 'math', 'template'}
_SAFE_HREF = re.compile(r'^(?:https?:|mailto:|sip:|sips:|tel:)', re.I)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROPPED_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in _ALLOWED_TAGS:
            return
        if tag == 'a':
            href = dict(attrs).get('href') or ''
            if _SAFE_HREF.match(href.strip()):
                self.out.append(f'<a href="{html.escape(href.strip(), quote=True)}">')
            else:
                self.out.append('<a>')
        else:
            self.out.append(f'<{tag}>')
        if tag not in _VOID_TAGS:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in _VOID_TAGS and not self.skip:
            self.out.append(f'<{tag}>')

    def handle_endtag(self, tag):
        if tag in _DROPPED_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.open:
            return
        while self.open:
            closing = self.open.pop()
            self.out.append(f'</{closing}>')
            if closing == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def sanitize_html(body):
    """HTML a peer sent, reduced to formatting and safe links: no scripts, styles, images,
    event handlers or attributes other than a link's http(s)/mailto/sip/tel href."""
    parser = _Sanitizer()
    try:
        parser.feed(body or '')
        parser.close()
    except Exception:
        return linkify(re.sub(r'<[^>]+>', '', body or ''))
    while parser.open:
        parser.out.append(f'</{parser.open.pop()}>')
    return ''.join(parser.out)


def day_label(day, today, weekday_name=None, month_name=None):
    """The divider above a day's first message: Today, Yesterday, the weekday within the
    last week, else day and month (and the year when it is not this year). weekday_name and
    month_name (callables taking 1-based numbers) give localised names; English by default."""
    days = (today - day).days
    if days == 0:
        return 'Today'
    if days == 1:
        return 'Yesterday'
    if 1 < days < 7:
        return weekday_name(day.isoweekday()) if weekday_name else day.strftime('%A')
    month = month_name(day.month) if month_name else day.strftime('%B')
    return f'{day.day} {month}' if day.year == today.year else f'{day.day} {month} {day.year}'


# What an outgoing message's state shows after its time: (mark, kind); kind picks the colour.
_DELIVERY_MARKS = {
    'pending': ('🕑', 'pending'),
    'accepted': ('✓', 'sent'),
    'sent': ('✓', 'sent'),
    'delivered': ('✔', 'delivered'),
    'displayed': ('✔✔', 'displayed'),
    'failed': ('⚠', 'failed'),
    'failed-local': ('⚠', 'failed'),
    'error': ('⚠', 'failed'),
}


def delivery_mark(item):
    """(mark, kind) for an outgoing message's delivery state, or ('', None)."""
    if item.direction != 'outgoing':
        return '', None
    return _DELIVERY_MARKS.get(str(item.state or ''), ('', None))


MiB = 1024 * 1024

# What is fetched without being asked, by kind: the largest size, and for audio and video how old it may be.
AUTO_FETCH_LIMITS = {'image': 8 * MiB, 'pdf': 10 * MiB, 'video': 20 * MiB, 'audio': 10 * MiB}
AUTO_FETCH_RECENT_DAYS = 7
AUTO_FETCH_RECENT_ONLY = ('video', 'audio')


def auto_fetch_reason(category, filename, size, age_days):
    """None when a file in view is fetched on its own, else why it waits for a click:
    pictures up to 8 MiB, PDFs up to 10 MiB, videos up to 20 MiB and audio (voice
    notes) up to 10 MiB when a week old at most; other files never."""
    name = str(filename or '').lower()
    if name.endswith('.asc'):
        name = name[:-4]
    kind = 'pdf' if name.endswith('.pdf') else category
    limit = AUTO_FETCH_LIMITS.get(kind)
    if limit is None:
        return 'not fetched without a click'
    if size is None or size <= 0:
        return 'size unknown'
    if size > limit:
        return f'larger than {limit // MiB} MiB'
    if kind in AUTO_FETCH_RECENT_ONLY and age_days is not None and age_days > AUTO_FETCH_RECENT_DAYS:
        return f'older than {AUTO_FETCH_RECENT_DAYS} days'
    return None


def format_size(size):
    """A file size for people: 512 bytes, 12 KB, 3.4 MB, 1.2 GB (powers of 1024, as file managers here show them)."""
    if size is None or size < 0:
        return ''
    if size < 1024:
        return f'{size} bytes'
    for unit in ('KB', 'MB', 'GB', 'TB'):
        size /= 1024.0
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if size >= 100 or unit == 'KB' else f'{size:.1f} {unit}'
