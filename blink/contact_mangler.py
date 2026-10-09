"""Runtime-only pseudonyms for contacts, for screenshots.

Screenshots for the web site and the stores want pictures of the application with a populated contact
list and a real conversation in it -- and a real contact list is a page of
other people's addresses. Emptying it first gives an empty-looking app;
editing every contact by hand changes the address book for good and syncs
the edit to XCAP.

So this maps each real identity to an invented one for as long as the
application runs. Nothing here is stored, nothing is written back: the
mapping lives in this module's memory and is applied where a name or an
address is about to be DRAWN, never where one is saved, sent or matched.
That is the whole safety property -- a mangled name must not be able to
reach the address book, the message history or the wire, so no call site
may feed a mangled value back into a model object.

The invented identity is a plausible person rather than an obvious
placeholder ("Alice Bennett", not "Contact 1"): a screenshot full of
contact1@ reads as a demo, which is exactly what the picture is trying not
to look like. The DOMAIN is kept as it is -- user@sylk.link becomes
alice.bennett@sylk.link -- because the domain is the service being
advertised and is not anybody's private detail.

The mapping is derived from a hash of the real address, so the same
contact gets the same invented one on every launch and a screenshot can be
retaken next week and still match the ones beside it.
"""

__all__ = ['mangling_enabled', 'invalidate', 'refresh_views', 'mangled_name', 'mangled_uri',
           'mangled_text', 'mangled_icon_path', 'mangled_account_label',
           'mangled_username']

import re
import zlib

from threading import RLock


# Two pools, crossed: 768 invented people, which is more than any address
# book this is pointed at. Deliberately spread across languages, the way a
# real contact list of a communications client looks.
FIRST_NAMES = ('Alice', 'Bruno', 'Carla', 'Diego', 'Elena', 'Felix', 'Greta',
               'Hugo', 'Irina', 'Jonas', 'Karin', 'Lucas', 'Maria', 'Niels',
               'Olga', 'Pablo', 'Rosa', 'Simon', 'Tessa', 'Ulrich', 'Vera',
               'Willem', 'Xenia', 'Yannick', 'Zoe', 'Anton', 'Bianca',
               'Cedric', 'Dalia', 'Emil', 'Fiona', 'Gustav')

LAST_NAMES = ('Bennett', 'Cortez', 'Dahl', 'Eriksen', 'Ferrari', 'Garzon',
              'Hoekstra', 'Ivanov', 'Jansen', 'Keller', 'Lombardi', 'Moreau',
              'Novak', 'Oliveira', 'Petrov', 'Quintana', 'Ramos', 'Silva',
              'Torres', 'Ubach', 'Vermeer', 'Walsh', 'Yilmaz', 'Zanetti')

_COMBINATIONS = len(FIRST_NAMES) * len(LAST_NAMES)

_SCHEMES = ('sips:', 'sip:', 'mailto:', 'xmpp:', 'tel:')

# An address inside a longer line ("Private message from bob@example.com").
_embedded_uri_re = re.compile(r'(?:sips?:|mailto:)?[\w.!~*\'+%-]+@[\w.-]+\.[A-Za-z]{2,}')
# A run that could be a telephone number. Whether it really is one is
# decided by the digit count in the replacement, so that dates and times
# in a history detail line are left alone.
_embedded_number_re = re.compile(r'\+?\d[\d\s().-]{4,}\d')
_phone_re = re.compile(r'^\+?[\d\s().-]+$')
_word_re = re.compile(r"[^\W\d_]+", re.UNICODE)

_lock = RLock()
_identities = {}                # canonical key -> _Identity
_used_names = set()             # invented full names already handed out
# Everything this module has already produced, so a value that has been
# through it once is left alone the second time. Without this the generic
# sweep in mangled_text() mangles the substitution the specific pass just
# made, and the same contact comes out as two different people on two
# different lines.
_produced = set()
_enabled = None                 # tri-state: None means 'ask the settings'


class _Identity(object):
    """One invented person, standing in for one real address."""

    __slots__ = ('name', 'username', 'digits')

    def __init__(self, name, username, digits):
        self.name = name
        self.username = username
        self.digits = digits

    def __repr__(self):
        return '<_Identity %s (%s)>' % (self.name, self.username)


# -- the setting -----------------------------------------------------------

def mangling_enabled():
    """Whether Preferences -> Interface -> Mangle Contacts is on.

    Cached, because this is asked once per drawn row: the contact list
    repaints the whole visible column on every presence change. The cache
    is dropped by invalidate(), which the settings notification calls.
    """
    global _enabled
    if _enabled is None:
        try:
            from blink.configuration.settings import BlinkSettings
            _enabled = bool(BlinkSettings().interface.mangle_contacts)
        except Exception:
            # Before the settings exist there is nothing to hide yet.
            return False
    return _enabled


def invalidate():
    """Re-read the setting on the next question.

    The mapping itself is kept: turning the setting off and on again
    during a screenshot session has to give the same invented people, or
    the pictures taken either side of it will not match.
    """
    global _enabled
    with _lock:
        _enabled = None


# -- the mapping -----------------------------------------------------------

def _strip_scheme(text):
    low = text.lower()
    for scheme in _SCHEMES:
        if low.startswith(scheme):
            return text[:len(scheme)], text[len(scheme):]
    return '', text


def _split_uri(text):
    """(scheme, user, rest), where rest starts at the '@' and is kept whole.

    Everything after the '@' -- domain, port, ;parameters -- is carried
    across untouched. Only the user part is anybody's private detail.
    """
    scheme, body = _strip_scheme(str(text or '').strip())
    if '@' in body:
        user, _, rest = body.partition('@')
        return scheme, user, '@' + rest
    return scheme, body, ''


def _canonical(uri):
    """The key an address is filed under: scheme-less, parameter-less, lower.

    The same person arrives as 'sip:bob@example.com', 'bob@example.com' and
    'sip:bob@example.com;transport=tls' from three different callers, and
    all three have to map to one invented person.
    """
    _scheme, body = _strip_scheme(str(uri or '').strip())
    body = body.split(';')[0].split('?')[0]
    return body.lower()


def _is_phone(text):
    value = str(text or '').strip()
    if not value or not _phone_re.match(value):
        return False
    return len(re.sub(r'\D', '', value)) >= 5


def _digits_from(seed, count):
    """`count` deterministic digits derived from a key."""
    out = []
    salt = 0
    while len(out) < count:
        chunk = zlib.crc32(('%s#%d' % (seed, salt)).encode('utf-8')) & 0xffffffff
        out.extend(str(chunk).zfill(10))
        salt += 1
    return ''.join(out[:count])


def _remember(value):
    """File a produced value, so it is recognised as already mangled."""
    with _lock:
        text = str(value).strip()
        _produced.add(text)
        _produced.add(_canonical(text))
        # The user part on its own too: a detail line carries a telephone
        # number without its domain, and the generic sweep that follows a
        # specific substitution must recognise it as already done.
        user = _split_uri(text)[1]
        if user:
            _produced.add(user)
    return value


def _make_identity(key):
    """Invent a person for this key, avoiding one already handed out."""
    start = zlib.crc32(key.encode('utf-8')) & 0xffffffff
    full = None
    for probe in range(_COMBINATIONS):
        # 7919 is coprime with the pool size, so probing visits every
        # combination before repeating one.
        index = (start + probe * 7919) % _COMBINATIONS
        first = FIRST_NAMES[index % len(FIRST_NAMES)]
        last = LAST_NAMES[index // len(FIRST_NAMES)]
        candidate = '%s %s' % (first, last)
        if candidate not in _used_names:
            full = candidate
            break
    if full is None:
        # More contacts than invented people: number the overflow rather
        # than hand two contacts the same face.
        index = start % _COMBINATIONS
        first = FIRST_NAMES[index % len(FIRST_NAMES)]
        last = LAST_NAMES[index // len(FIRST_NAMES)]
        full = '%s %s %d' % (first, last, len(_used_names))
    _used_names.add(full)
    parts = full.lower().split()
    username = '.'.join(parts[:2])
    return _Identity(full, username, _digits_from(key, 12))


def _identity(uri=None, name=None):
    """The invented person standing in for this address, or for this name.

    One key per person, whichever way they arrive: an address is filed
    under itself, and somebody known only by a display name is filed under
    that -- so the header, the row and the bubble, which are handed the
    same contact in three different shapes, all land on one identity.
    """
    key = ''
    candidate = str(uri or '').strip()
    if candidate:
        bare = _canonical(candidate)
        user = _split_uri(candidate)[1]
        if _is_phone(user):
            # A telephone number is one person whether it arrives bare, as
            # tel:, or as a user part on somebody's PSTN gateway, so it is
            # filed under its digits alone.
            key = 'tel:%s' % re.sub(r'\D', '', user)
        elif '@' in bare:
            key = bare
    if not key:
        label = str(name or '').strip() or candidate
        key = ('name:%s' % label.lower()) if label else 'anonymous'
    with _lock:
        identity = _identities.get(key)
        if identity is None:
            identity = _make_identity(key)
            _identities[key] = identity
        return identity


# -- what the call sites use ----------------------------------------------

def mangled_uri(uri, identity=None):
    """An address as it may be shown: invented user part, real domain."""
    if uri is None:
        return uri
    # Off means untouched, and untouched means the same object.
    if not mangling_enabled():
        return uri
    text = str(uri)
    if not text.strip():
        return uri
    scheme, user, rest = _split_uri(text)
    if not user:
        return text
    with _lock:
        if _canonical(text) in _produced or text.strip() in _produced:
            return text
    identity = identity or _identity(uri=text, name=text)
    if not rest and not _is_phone(user):
        # Not an address at all -- a display name that reached a uri slot.
        return identity.name
    if _is_phone(user):
        mangled = scheme + _mangled_number(user, identity) + rest
    else:
        mangled = scheme + identity.username + rest
    return _remember(mangled)


def _mangled_number(text, identity):
    """A telephone number of the same shape and a different subscriber.

    The leading '+' and the first two digits survive, so a Dutch number
    still looks Dutch in the picture; everything that identifies the
    subscriber is replaced. Separators stay where they were.
    """
    digits = re.sub(r'\D', '', text)
    keep = 2 if len(digits) > 6 else 0
    pool = identity.digits
    replacement = digits[:keep] + ''.join(
        pool[i % len(pool)] for i in range(len(digits) - keep))
    out = []
    index = 0
    for char in str(text):
        if char.isdigit():
            out.append(replacement[index])
            index += 1
        else:
            out.append(char)
    return ''.join(out)


def mangled_name(name, uri=None):
    """A display name as it may be shown.

    A name that is really an address (a contact nobody has named yet shows
    its address on the first line) is mangled as an address, so the row
    does not gain a person who is not in the address book.
    """
    if name is None:
        return name
    if not mangling_enabled():
        return name
    text = str(name)
    if not text.strip():
        return name
    stripped = text.strip()
    if '@' in stripped or _is_phone(stripped):
        return mangled_uri(stripped, identity=_identity(uri=uri or stripped))
    with _lock:
        if stripped in _produced:
            return name
    return _remember(_identity(uri=uri, name=stripped).name)


def mangled_text(text, uri=None, name=None):
    """A free-form line that may have an address or a name inside it.

    The contact list's second line is built by its group -- 'Missed call
    12/09/2026 14:03', 'bob@example.com (work)', a presence note -- so
    there is no single field to replace. What is known about the contact
    is substituted first, then anything left that still looks like an
    address or a subscriber number.
    """
    if text is None:
        return text
    if not mangling_enabled():
        return text
    value = str(text)
    if not value.strip():
        return text

    identity = _identity(uri=uri, name=name) if (uri or name) else None

    if uri:
        real = str(uri).strip()
        if real:
            replacement = mangled_uri(real, identity=identity)
            value = value.replace(real, replacement)
            # ...and again without the scheme, because the line may carry
            # the address in either spelling.
            bare = _strip_scheme(real)[1]
            if bare and bare != real:
                value = value.replace(bare, _strip_scheme(replacement)[1])
            # A contact reached on a telephone number is filed under the
            # whole address, and its detail line carries the number on its
            # own. Only for a number: a user part of letters is too short
            # a string to substitute blindly inside a sentence.
            user = _split_uri(real)[1]
            if user and _is_phone(user) and user in value:
                value = value.replace(user, _split_uri(replacement)[1])
    if name and identity is not None:
        real_name = str(name).strip()
        # Only a name with a letter in it: a 'name' that is really the
        # address was handled above, and replacing a bare number here
        # would hit the timestamps.
        if real_name and _word_re.search(real_name) and real_name in value:
            value = value.replace(real_name, identity.name)

    value = _embedded_uri_re.sub(lambda match: mangled_uri(match.group(0)), value)
    value = _embedded_number_re.sub(_mangle_number_match, value)
    return value


def _mangle_number_match(match):
    """Mangle a matched run only if it really looks like a phone number."""
    text = match.group(0)
    digits = re.sub(r'\D', '', text)
    if len(digits) < (7 if text.startswith('+') else 8):
        return text                 # a date, a time, a message count
    with _lock:
        if text.strip() in _produced:
            return text             # already an invented number
    return _remember(_mangled_number(text, _identity(uri=text)))


def mangled_icon_path(path):
    """No photographs while mangling.

    A face in a screenshot names somebody as surely as their address does,
    and the drawing code already falls back to initials on a colour when
    there is no image -- which is what the invented person should have.
    """
    return None if mangling_enabled() else path


def mangled_username(value):
    """A bare user part, with no domain attached to identify it.

    The authentication username is the one field that carries somebody's
    address with the '@domain' cut off, so the generic sweep cannot see
    it for what it is and the caller has to say so.
    """
    if value is None:
        return value
    if not mangling_enabled():
        return value
    text = str(value)
    if not text.strip():
        return value
    stripped = text.strip()
    with _lock:
        if stripped in _produced:
            return value
        # If an address with this user part has already been mangled --
        # and it has, because the pane shows the account's own address
        # above this field -- reuse that person. An account whose
        # authentication username belonged to somebody else would be a
        # strange thing to photograph.
        for key, identity in _identities.items():
            if '@' in key and key.split('@', 1)[0] == stripped.lower():
                return _remember(identity.username)
    return _remember(_identity(name=stripped).username)


def mangled_account_label(label):
    """One of my own accounts, as the account menu and the pane pill show it.

    My own address is in every screenshot too, and it is the one address
    that is on the picture whatever the contact list is showing.
    """
    return mangled_name(label) if mangling_enabled() else label


def refresh_views():
    """Repaint every list after the setting changed: the contact rows are drawn through this module."""
    from PyQt6.QtWidgets import QAbstractItemView, QApplication
    for widget in QApplication.allWidgets():
        if isinstance(widget, QAbstractItemView):
            widget.viewport().update()
