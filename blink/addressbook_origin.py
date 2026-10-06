# Copyright (C) 2026 AG Projects. See LICENSE for details.
#
"""Who last changed an addressbook entry, and when.

Every contact and group this device saves carries, in the shared attributes
bag ('ag-projects:sipsimple', the one sylk mobile reads and writes):

    modified_by      bare instance id of the device that saved it
    modified_agent   that device's user agent, 'Blink 9.8.0 (MacOSX)'
    modified_at      UTC, ISO 8601 to the second, '2026-09-16T11:44:07Z'
    modified_reason  what made the save, when it was not the user: 'repair',
                     'call-history', ... -- empty for an ordinary edit
    modified_hash    fingerprint of the content the stamp vouches for

The hash is what makes the stamp trustworthy across clients that do not stamp.
The spec requires every client to carry the attributes bag through verbatim,
so a client that changes a contact without stamping it leaves OUR stamp on
content we never wrote. Recomputing the fingerprint on arrival tells the two
apart: a match is "changed by the device named", a mismatch is "changed by a
client that does not stamp, some time after the device named".

The fingerprint covers what the document says about the entry -- a contact's
name, addresses (value and type, order ignored), presence and dialog handling;
a group's name and member ids -- and none of the attributes. Attributes are
out because the bag holds values this client does not register (the mobile's
'keys', for one) and defaults it does not write, so a hash over them would
disagree between the object saved here and the same object read back.

Stamping happens in save() and only there, which is only ever a local write:
applying a fetched document goes through _internal_save with a Remote
originator and never calls save(), so the stamp of whoever wrote the document
is what gets copied into the other accounts' documents, not ours.

Nothing here touches Qt or the network. Ported from Blink for macOS
AddressbookOrigin.py; keep the two the same so fixes carry over by diffing.
"""

import functools
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager

__all__ = ['STAMP_KEYS', 'reason', 'with_reason', 'current_reason',
           'contact_fingerprint', 'group_fingerprint', 'install',
           'describe_origin', 'diff_document', 'format_changes',
           'load_snapshot', 'save_snapshot']


STAMP_KEYS = ('modified_by', 'modified_agent', 'modified_at', 'modified_reason', 'modified_hash')
SNAPSHOT_VERSION = 1


# --- why a save happened ----------------------------------------------------
# Per thread, innermost wins: backfill_call_contacts calling ensure_call_contact
# is stamped with whichever of the two is closer to the save.

_local = threading.local()


@contextmanager
def reason(text):
    stack = getattr(_local, 'stack', None)
    if stack is None:
        stack = _local.stack = []
    stack.append(str(text or ''))
    try:
        yield
    finally:
        stack.pop()


def with_reason(text):
    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            with reason(text):
                return function(*args, **kwargs)
        return wrapper
    return decorator


def current_reason():
    stack = getattr(_local, 'stack', None)
    return stack[-1] if stack else ''


# --- fingerprints -----------------------------------------------------------
# Take the XCAP-side objects (sipsimple.account.xcap Contact / Group, which is
# also what XCAPContact / XCAPGroup and a fetched document are made of).

def _text(value):
    return '' if value is None else str(value)


def _bool(value):
    # XCAP attribute text and real booleans both reach us; 'false' is not true.
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1')
    return bool(value)


def _event(handling):
    # A missing handling is what the server stores for it: policy 'default',
    # not subscribed. sylk-mobile's addressbookOrigin.js applies the same rule.
    policy = getattr(handling, 'policy', None)
    return [_text('default' if policy is None else policy), _bool(getattr(handling, 'subscribe', False))]


def _digest(body):
    data = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha1(data.encode('utf-8')).hexdigest()[:16]


def contact_fingerprint(contact):
    uris = sorted([_text(getattr(uri, 'uri', None)).strip(), _text(getattr(uri, 'type', None))]
                  for uri in (getattr(contact, 'uris', None) or ()))
    return _digest({'name': _text(getattr(contact, 'name', None)),
                    'uris': uris,
                    'presence': _event(getattr(contact, 'presence', None)),
                    'dialog': _event(getattr(contact, 'dialog', None))})


def _member_ids(group):
    contacts = getattr(group, 'contacts', None) or ()
    if hasattr(contacts, 'ids'):
        return [_text(value) for value in contacts.ids()]
    return [_text(getattr(item, 'id', item)) for item in contacts]


def group_fingerprint(group):
    return _digest({'name': _text(getattr(group, 'name', None)),
                    'contacts': sorted(_member_ids(group))})


def utc_timestamp(now=None):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() if now is None else now))


# --- send side --------------------------------------------------------------

_installed = [False]


def _stamp(obj, device_id, agent, fingerprint, now=None):
    if not hasattr(obj, 'modified_by'):
        return False        # extension not registered: nothing to write into
    obj.modified_by = _text(device_id)
    obj.modified_agent = _text(agent)
    obj.modified_at = utc_timestamp(now)
    obj.modified_reason = current_reason()
    obj.modified_hash = fingerprint
    return True


def _wrap_save(cls, previous_attribute, fingerprint, device_id, agent, log):
    original = cls.save

    def save(self):
        try:
            state = getattr(self, '__state__', None)
            if state != 'deleted':
                previous = getattr(self, previous_attribute, None)
                current = fingerprint(self.__toxcap__())
                # Stamp only a change the document will see. A save that moved
                # nothing but local settings (an icon, a spell-check language)
                # must not claim the contact for this device.
                if state == 'new' or previous is None or fingerprint(previous) != current:
                    _stamp(self, device_id(), agent(), current)
        except Exception as e:
            if log is not None:
                log('[ab] [origin] cannot stamp %s: %s' % (getattr(self, 'id', '?'), e))
        return original(self)

    functools.update_wrapper(save, original)
    cls.save = save


def install(contact_class, group_class, device_id, agent, log=None):
    """Wrap Contact.save and Group.save so local writes carry a stamp.

    device_id and agent are callables, read at save time: the user agent is
    set after the extensions are registered.
    """
    if _installed[0]:
        return
    _installed[0] = True
    _wrap_save(contact_class, '__xcapcontact__', contact_fingerprint, device_id, agent, log)
    _wrap_save(group_class, '__xcapgroup__', group_fingerprint, device_id, agent, log)


# --- receive side -----------------------------------------------------------

def _attributes(obj):
    attributes = getattr(obj, 'attributes', None) or {}
    return dict((key, _text(attributes.get(key))) for key in STAMP_KEYS)


def describe_origin(obj, fingerprint, this_device=None):
    stamp = _attributes(obj)
    if not stamp['modified_by'] and not stamp['modified_hash']:
        return 'by a client that does not stamp (never stamped)'
    if this_device and stamp['modified_by'] == this_device:
        who = 'this device'
    else:
        who = '%s device %s' % (stamp['modified_agent'] or '?', stamp['modified_by'] or '?')
    when = stamp['modified_at'] or '?'
    why = ' (%s)' % stamp['modified_reason'] if stamp['modified_reason'] else ''
    if stamp['modified_hash'] == fingerprint(obj):
        return 'by %s at %s%s' % (who, when, why)
    return ('by a client that does not stamp, after the last stamp: %s at %s%s'
            % (who, when, why))


def _entries(addressbook, collection, fingerprint):
    entries = {}
    for item in (getattr(addressbook, collection, None) or ()):
        entries[_text(item.id)] = (item, fingerprint(item))
    return entries


def diff_document(addressbook, previous, this_device=None):
    """Compare a fetched document against the snapshot of the last one.

    Returns (changes, snapshot). changes is None when there was no snapshot to
    compare against -- the first document ever seen is a baseline, not a list
    of additions.
    """
    snapshot = {'v': SNAPSHOT_VERSION, 'contacts': {}, 'groups': {}}
    changes = [] if previous else None
    for kind, collection, fingerprint in (('contact', 'contacts', contact_fingerprint),
                                          ('group', 'groups', group_fingerprint)):
        current = _entries(addressbook, collection, fingerprint)
        before = (previous or {}).get(collection) or {}
        for id, (item, digest) in current.items():
            name = _text(getattr(item, 'name', None))
            snapshot[collection][id] = [digest, name]
            if changes is None:
                continue
            known = before.get(id)
            if known is not None and known[0] == digest:
                continue
            changes.append({'op': 'add' if known is None else 'update', 'kind': kind, 'id': id,
                            'name': name, 'was': None if known is None else known[1],
                            'origin': describe_origin(item, fingerprint, this_device)})
        if changes is not None:
            for id, known in before.items():
                if id not in current:
                    # A deleted entry takes its stamp with it.
                    changes.append({'op': 'remove', 'kind': kind, 'id': id, 'name': known[1],
                                    'was': None, 'origin': 'by an unknown device (a removal carries no stamp)'})
    return changes, snapshot


def format_changes(changes, cap=50):
    lines = []
    for change in changes[:cap]:
        renamed = (' (was %r)' % change['was']
                   if change['was'] is not None and change['was'] != change['name'] else '')
        lines.append('%s %s %s %r%s -- %s' % (change['op'], change['kind'], change['id'],
                                               change['name'], renamed, change['origin']))
    if len(changes) > cap:
        lines.append('... %d more not listed (cap %d)' % (len(changes) - cap, cap))
    return lines


def load_snapshot(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('v') != SNAPSHOT_VERSION:
        return None
    return data


def save_snapshot(path, snapshot):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = '%s.tmp' % path
    with open(temporary, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(temporary, path)
