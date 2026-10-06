"""Repairs of addressbook contacts, decided without touching the addressbook.

Ported from Blink for macOS HistoryManager (server_conference_uri,
echoed_name_replacement, repair_contact_addresses); keep the rules the same.
blink.contacts.ContactRepair applies the plan once per run after the
addressbook has loaded. Sylk Mobile repairs the same things from its side; the
two are idempotent with respect to each other.

No Qt and no sipsimple: a contact is anything with name and uris (each with
uri, and id).
"""

__all__ = ['server_conference_uri', 'echoed_name_replacement', 'repair_plan']

from types import SimpleNamespace

from blink.pstn_normalize import is_conference_uri, pstn_e164, same_phone_number, sip_prefix_pattern


def server_conference_uri(uri):
    """A conference room's address on the bridge domain, from any spelling of it.

    'videoconference.X' is Sylk Mobile's client-local view of a bridge; it swaps
    it back to 'conference.X' for the server, and a room that reached the shared
    document under the local domain cannot be dialled. Rewritten by prefix, so
    it does not depend on knowing the account's bridge. Unchanged otherwise.
    """
    if not uri:
        return uri
    text = uri.decode() if isinstance(uri, bytes) else str(uri)
    stripped = sip_prefix_pattern.sub('', text.strip())
    if '@' not in stripped:
        return text
    user, _, domain = stripped.partition('@')
    if not domain.lower().startswith('videoconference.'):
        return text
    return '%s@conference.%s' % (user, domain[len('videoconference.'):])


def echoed_name_replacement(contact):
    """The name this contact should have when its name is only its address, or None.

    A contact named '+31618853125@sylk.link' at '+31618853125' wears an old
    spelling of its own address. A room is named by its room number; anything
    else by its address. A real name is never changed: it has no domain to
    strip and is not a phone number in another spelling.
    """
    name = str(getattr(contact, 'name', '') or '').strip()
    if not name:
        return None
    try:
        uris = [str(uri.uri).strip() for uri in contact.uris if str(uri.uri).strip()]
    except Exception:
        return None
    if not uris:
        return None
    lowered = {uri.lower(): uri for uri in uris}
    lowered_name = name.lower()

    def replacement(address):
        if is_conference_uri(address):
            room = address.partition('@')[0]
            return room if room and room != name else None
        return address if address != name else None

    # the name IS one of the addresses: only a room changes, to its number
    if lowered_name in lowered:
        return replacement(lowered[lowered_name])

    # the name is the number in another spelling ('0034913336701' at '+34913336701'),
    # only against addresses that really are phone numbers (not extensions)
    if any(char.isdigit() for char in lowered_name):
        for address in lowered.values():
            if pstn_e164(address) and same_phone_number(lowered_name, address):
                return replacement(address)

    # otherwise only a name that looks like an address counts
    if '@' not in lowered_name:
        return None
    local_part = lowered_name.partition('@')[0]
    if not local_part:
        return None
    if local_part in lowered:                         # the address is stored bare, as numbers are
        return replacement(lowered[local_part])
    for lowered_uri, address in lowered.items():      # same party under a repaired domain
        if lowered_uri.partition('@')[0] == local_part:
            return replacement(address)
    return None


def repair_plan(contact, account=None):
    """What repairing one contact would change, without changing it.

    Returns a dict, empty when nothing is to be done:
      'addresses':  [(uri, current, wanted, reason)]   reason 'conference-domain' or 'e164'
      'name':       (current, wanted)                  reason 'echoed-name'
      'duplicates': [(uri, kept_uri)]                  reason 'dup-uri'
    The name and the duplicates are judged on the addresses as they will be,
    so two spellings of one number become one, and a name echoing the old
    spelling follows it, in the same pass. Only a number pstn_e164 resolves
    is rewritten: anything else would be rewritten just to change its case.
    """
    addresses = []
    for uri in list(contact.uris):
        current = str(uri.uri)
        wanted = server_conference_uri(current)
        reason = 'conference-domain'
        if wanted == current:
            e164 = pstn_e164(current, account)
            if e164:
                wanted, reason = e164, 'e164'
        if wanted != current:
            addresses.append((uri, current, wanted, reason))
    wanted_by_uri = {id(uri): wanted for uri, current, wanted, reason in addresses}
    future = [SimpleNamespace(uri=wanted_by_uri.get(id(uri), str(uri.uri)), id=getattr(uri, 'id', None), original=uri) for uri in contact.uris]

    plan = {}
    if addresses:
        plan['addresses'] = addresses
    renamed = echoed_name_replacement(SimpleNamespace(name=getattr(contact, 'name', ''), uris=future))
    if renamed:
        plan['name'] = (str(getattr(contact, 'name', '') or ''), renamed)
    survivors, duplicates = {}, []
    for item in future:
        key = str(item.uri).strip().lower()
        if not key:
            continue
        if key in survivors:
            duplicates.append((item.original, survivors[key].original))
        else:
            survivors[key] = item
    if duplicates:
        plan['duplicates'] = duplicates
    return plan
