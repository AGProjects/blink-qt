"""Profiles: sets of accounts, each with its own address book and settings.

A profile is the configuration file (<data>/config: the accounts, their address
book, the general settings) and the call history (<data>/calls_history). The one
in use stays where Blink and the SDK read it, in the data directory; the others
are kept in <data>/profiles/<name>/. Everything else is shared: the message
database (its rows carry the account, and what is shown is read for the accounts
in use), keys, logs, downloads.

Delete Profile deletes the profile in use: Blink restarts in another one and the
deleted profile is put aside in <data>/profiles/.deleted/<name>-<time>/, never erased.

A switch takes a restart: it is asked for (<data>/profiles/switch) and done at the
next start, before anything reads the configuration: the files in use go to
the current profile's folder and the chosen profile's come out of its own (a new
profile starts with the general settings of the one it was made from, no
accounts and no address book). The name of the profile in use is in
<data>/profiles/active ("Default" until it is given another).
"""

import os
import re

from blink.resources import ApplicationData


__all__ = ['current_profile', 'profile_names', 'request_switch', 'request_delete_current', 'apply_pending_switch', 'rename_current', 'create_profile', 'accounts_of_current', 'valid_name']


DEFAULT_NAME = 'Default'
PROFILE_FILES = ('config', 'calls_history')         # what a profile is made of, in the data directory
SHARED_SECTIONS_DROPPED = ('Accounts', 'Addressbook')   # what a new profile does not take from the current one


def _folder():
    return ApplicationData.get('profiles')


def _path(*parts):
    return os.path.join(_folder(), *parts)


def valid_name(name):
    name = (name or '').strip()
    return bool(name) and len(name) <= 64 and not name.startswith('.') and re.fullmatch(r'[^/\\:\x00-\x1f]+', name) is not None and name not in ('active', 'switch', 'delete')


def _read(path):
    try:
        with open(path, encoding='utf-8') as file:
            return file.read().strip()
    except OSError:
        return ''


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        file.write(text)
    os.replace(temporary, path)


def current_profile():
    name = _read(_path('active'))
    return name if valid_name(name) else DEFAULT_NAME


def profile_names():
    """All profiles, the one in use among them, sorted."""
    names = {current_profile()}
    try:
        names.update(name for name in os.listdir(_folder()) if os.path.isdir(_path(name)) and valid_name(name))
    except FileNotFoundError:
        pass
    return sorted(names, key=str.lower)


def request_switch(name):
    """Use profile `name` from the next start on (Blink restarts to do it)."""
    if not valid_name(name):
        raise ValueError(f'invalid profile name: {name!r}')
    _write(_path('switch'), name)


def rename_current(name):
    """Give the profile in use another name (Save Profile As)."""
    if not valid_name(name):
        raise ValueError(f'invalid profile name: {name!r}')
    old = current_profile()
    if name == old:
        return
    if os.path.exists(_path(name)):
        raise FileExistsError(f'a profile named {name} exists')
    if os.path.isdir(_path(old)):
        os.rename(_path(old), _path(name))      # its folder is empty or not there: the files are in use
    _write(_path('active'), name)


def create_profile(name):
    """A new profile with the general settings of the one in use and nothing else."""
    if not valid_name(name):
        raise ValueError(f'invalid profile name: {name!r}')
    if name == current_profile() or os.path.exists(_path(name)):
        raise FileExistsError(f'a profile named {name} exists')
    from sipsimple.configuration.backend.file import FileBackend
    os.makedirs(_path(name))
    config = ApplicationData.get('config')
    if os.path.exists(config):
        data = FileBackend(config).load()
        for section in SHARED_SECTIONS_DROPPED:
            data.pop(section, None)
        FileBackend(_path(name, 'config')).save(data)


def request_delete_current(switch_to):
    """Delete the profile in use: Blink restarts in `switch_to` and the profile it leaves is
    put aside at that start, when its files are no longer in use."""
    if switch_to == current_profile():
        raise ValueError('cannot switch to the profile being deleted')
    request_switch(switch_to)
    _write(_path('delete'), current_profile())


def _put_aside(name):
    """A deleted profile is never erased: it is moved to profiles/.deleted/<name>-<time>/,
    where it can be taken back from (or removed by hand)."""
    from datetime import datetime
    source = _path(name)
    if not os.path.isdir(source):
        return None
    target = _path('.deleted', f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.rename(source, target)
    return target


def accounts_of_current():
    """The SIP accounts of the profile in use, from its configuration file."""
    config = ApplicationData.get('config')
    try:
        from sipsimple.configuration.backend.file import FileBackend
        accounts = FileBackend(config).load().get('Accounts') or {}
        return sorted(key for key in accounts if key != 'bonjour')
    except Exception:
        return []


def has_accounts():
    """Whether the configuration in use has SIP accounts (a new profile has none until one is added)."""
    config = ApplicationData.get('config')
    if not os.path.exists(config):
        return False
    try:
        from sipsimple.configuration.backend.file import FileBackend
        accounts = FileBackend(config).load().get('Accounts') or {}
        return any(key != 'bonjour' for key in accounts)
    except Exception:
        return True


def apply_pending_switch():
    """At start, before the configuration is read: make the profile asked for the one in use.
    Returns (old, new) when a switch was made, else None."""
    target = _read(_path('switch'))
    if not target:
        return None
    try:
        os.unlink(_path('switch'))
    except OSError:
        pass
    current = current_profile()
    if not valid_name(target) or target == current:
        return None
    # the files in use go to the current profile's folder ...
    os.makedirs(_path(current), exist_ok=True)
    for name in PROFILE_FILES:
        source = ApplicationData.get(name)
        if os.path.exists(source):
            os.replace(source, _path(current, name))
    # ... and the chosen profile's come out of its own (missing ones start empty)
    for name in PROFILE_FILES:
        source = _path(target, name)
        if os.path.exists(source):
            os.replace(source, ApplicationData.get(name))
    _write(_path('active'), target)
    try:
        os.rmdir(_path(target))         # empty now: the profile in use lives in the data directory
    except OSError:
        pass
    # the profile left behind was deleted (Delete Profile): put aside, not erased
    deleted = _read(_path('delete'))
    if deleted:
        try:
            os.unlink(_path('delete'))
        except OSError:
            pass
        if deleted == current:
            _put_aside(current)
    return current, target
