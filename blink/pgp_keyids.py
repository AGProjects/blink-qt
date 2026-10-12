# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Which PGP keys a message was encrypted to, and whose keys those are.

encrypted_to(armored) reads the key ids from the public-key encrypted session
key packets of an armored PGP message (no private key needed). An outgoing
message is normally encrypted to the recipient and to the sender's own key.

key_owners() maps every key id (primary key and subkeys) of the keys Blink
holds to the address they belong to: <keys_directory>/<address>.pubkey for
the contacts, <keys_directory>/private/<account>.pubkey for our own accounts.
It is cached and read again only when a key directory changes.

describe(key_ids) turns ids into lines for people: 'ID (address)' or
'ID (address, our key)' or 'ID (unknown key)'.
"""

__all__ = ['encrypted_to', 'key_owners', 'describe']

import os
import threading
import warnings


def encrypted_to(armored):
    """The key ids (16 hex digits, uppercase) an armored PGP message is encrypted to, or []."""
    if not isinstance(armored, str) or '-----BEGIN PGP MESSAGE-----' not in armored:
        return []
    try:
        import pgpy
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            message = pgpy.PGPMessage.from_blob(armored)
        return sorted(str(key_id).upper() for key_id in message.encrypters)
    except Exception:
        return []


_lock = threading.Lock()
_cache = {'stamp': None, 'owners': {}}


def _directories():
    from sipsimple.configuration.settings import SIPSimpleSettings
    directory = SIPSimpleSettings().chat.keys_directory.normalized
    return [(directory, False), (os.path.join(directory, 'private'), True)]


def _stamp(directories):
    stamp = []
    for directory, _own in directories:
        try:
            stamp.append((directory, os.stat(directory).st_mtime_ns))
        except OSError:
            stamp.append((directory, None))
    return tuple(stamp)


def key_owners():
    """{key id: (address, own)} for every key Blink holds."""
    try:
        directories = _directories()
    except Exception:
        return {}
    stamp = _stamp(directories)
    with _lock:
        if _cache['stamp'] == stamp:
            return _cache['owners']
    owners = {}
    try:
        import pgpy
    except ImportError:
        return owners
    for directory, own in directories:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith('.pubkey'):
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    key, _ = pgpy.PGPKey.from_file(os.path.join(directory, name))
            except Exception:
                continue
            address = name[:-len('.pubkey')]
            for key_id in [key.fingerprint.keyid] + list(key.subkeys):
                key_id = str(key_id).upper()
                if own or key_id not in owners:      # our own key wins over a copy filed as a contact's
                    owners[key_id] = (address, own)
    with _lock:
        _cache['stamp'] = stamp
        _cache['owners'] = owners
    return owners


def describe(key_ids):
    """'ID (address)' per key id, our own keys marked."""
    owners = key_owners()
    lines = []
    for key_id in key_ids:
        key_id = str(key_id).strip().upper()
        if not key_id:
            continue
        owner = owners.get(key_id)
        if owner is None:
            lines.append(f'{key_id} (unknown key)')
        else:
            address, own = owner
            lines.append(f'{key_id} ({address}, our key)' if own else f'{key_id} ({address})')
    return lines
