"""Canonical remote party addresses.

One canonicaliser for every key built from a remote address: conversation
keys, unread counts, contact matching, Messages group deduplication and the
history purge on contact deletion. Blink for macOS grew two of them
(SMSWindowManager._canonical_uri does E.164, ContactListModel.
_canonical_contact_uri does not) and mixing them in delete/expunge purged
history that a merged contact still claimed. Do not add a second one here;
extend canonical_uri instead.

Ported from Blink for macOS SMSWindowManager (_canonical_uri,
bare_instance_id, illegal_uri, isFileableAddress) and SMSViewController
(is_placeholder_uri). No Qt; sipsimple only for parsing, and only if present.
"""

__all__ = ['canonical_uri', 'bare_instance_id', 'is_placeholder_uri', 'illegal_uri', 'is_fileable_address']

from blink.pstn_normalize import canonical_pstn_uri, pstn_e164

try:
    from sipsimple.core import SIPURI
except ImportError:
    SIPURI = None


_SCHEMES = ('sips:', 'sip:', 'tel:')

# Hosts that mean "this address exists to be parsed, not dialled": a Bonjour
# neighbour who is not on the network, and a conversation reopened from
# history. bonjour.local is the current form (sip:<instance id>@bonjour.local);
# the loopback pair is what older builds wrote and still sits in stored rows.
_PLACEHOLDER_HOSTS = ('bonjour.local', '127.0.0.1', 'localhost')

_URN_UUID = 'urn:uuid:'


def _default_account():
    try:
        from sipsimple.account import AccountManager
        return AccountManager().default_account
    except Exception:
        return None


def _strip_scheme(text):
    lowered = text.lower()
    for scheme in _SCHEMES:
        if lowered.startswith(scheme):
            return text[len(scheme):]
    return text


def bare_instance_id(value):
    """A Bonjour neighbour's instance id in the one form everything files it under.

    settings.instance_id is a uuid4 URN ("urn:uuid:<uuid>") and that is what
    travels in the TXT record and in the instance_id URI parameter. It is
    stored bare: a key that appears in two spellings is two conversations.
    """
    text = str(value or '').strip()
    if text.lower().startswith(_URN_UUID):
        text = text[len(_URN_UUID):]
    return text


def canonical_uri(raw_uri, account=None):
    """The key a remote party is filed under.

    - display name form ("Alice <sip:alice@example.com>") -> the address
    - sip:/sips:/tel: scheme, ;parameters and ?headers removed
    - lowercased; the port is kept (user@host[:port])
    - a phone number in any spelling (0031..., +31..., 06... with the account
      dial rules, +31...@domain) -> bare E.164
    - a withheld caller (@guest. / @anonymous.) -> anonymous@anonymous.invalid
    - a Bonjour instance id -> bare uuid

    The account supplies the PSTN rules (idd_prefix, replace_leading_zero,
    prefix); when None the default account is used, as on macOS. Pass the
    account explicitly where it is known, so keys do not change when the user
    changes the default account.
    """
    if raw_uri is None:
        return ''
    text = raw_uri.decode('utf-8', 'replace') if isinstance(raw_uri, bytes) else str(raw_uri)
    text = text.strip()
    if '<' in text and '>' in text and text.index('<') < text.index('>'):
        text = text[text.index('<') + 1:text.index('>')].strip()
    text = _strip_scheme(text)
    text = text.split(';', 1)[0].split('?', 1)[0].strip()
    if not text:
        return ''
    if text.lower().startswith(_URN_UUID):
        return bare_instance_id(text).lower()
    if account is None:
        account = _default_account()
    return canonical_pstn_uri(text, account) or text.lower()


def is_placeholder_uri(uri):
    """Whether this address stands in for one we do not have (nothing is filed under it)."""
    text = _strip_scheme(str(uri or '').strip())
    host = text.split('@')[-1].split(';')[0].split(':')[0].strip().lower()
    return host in _PLACEHOLDER_HOSTS


def illegal_uri(uri):
    """Whether no contact may be created for this address.

    Conference rooms and gateway guests are not people, and anything that does
    not parse as a SIP URI cannot be written to the address book.
    """
    text = str(uri or '').strip()
    if not text:
        return True
    lowered = text.lower()
    if '@videoconference.' in lowered or '@guest.' in lowered:
        return True
    text = _strip_scheme(text)
    if SIPURI is not None:
        try:
            SIPURI.parse('sip:%s' % text)
        except Exception:
            return True
        return False
    # without sipsimple (tests): the shape SIPURI.parse would insist on
    user, at, host = text.rpartition('@')
    return not host or any(c.isspace() for c in text) or (at and not user)


def is_fileable_address(uri, account=None, bonjour_keys=()):
    """Whether this conversation key may become an address book contact.

    A created contact is saved, synced to XCAP and comes back on every launch,
    so this is asked before one is created, not after. Not fileable:
    - anything without a domain that is not a phone number (a Bonjour instance
      id, for one); a bare phone number IS an address, it is how the
      addressbook stores one
    - a placeholder address
    - a key belonging to a Bonjour neighbour (bonjour_keys are canonical keys)
    """
    key = str(uri or '').strip()
    if not key:
        return False
    if account is None:
        account = _default_account()
    if '@' not in key and not pstn_e164(key, account):
        return False
    if is_placeholder_uri(key):
        return False
    if bonjour_keys and canonical_uri(key, account) in bonjour_keys:
        return False
    return True
