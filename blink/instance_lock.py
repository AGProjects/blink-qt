"""One Blink per data directory.

Two instances on the same data directory overwrite each other's files: the SIP SIMPLE
configuration is kept in memory and written back whole, so one instance replaces the
accounts, address book and settings saved by the other (and a profile switch made by one
is undone by the other). The first instance holds an exclusive lock on <data>/lock for
its lifetime; a second one finds it taken and does not start.

The lock goes with the process: it is released when Blink exits or crashes, and before
the exec of a restart (the new image takes it again).
"""

import os

from blink.resources import ApplicationData

try:
    import fcntl
except ImportError:         # Windows: no guard
    fcntl = None


__all__ = ['acquire', 'release', 'holder']


_file = None


def _path():
    return ApplicationData.get('lock')


def holder():
    """The pid written by the instance holding the lock, or None."""
    try:
        with open(_path(), encoding='ascii') as file:
            return int(file.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


def acquire():
    """Take the lock. Returns True when this is the only instance on the data directory."""
    global _file
    if fcntl is None or _file is not None:
        return True
    os.makedirs(ApplicationData.directory, exist_ok=True)
    file = open(_path(), 'a+', encoding='ascii')     # not truncated before we hold it: the holder's pid stays readable
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        return False
    file.seek(0)
    file.truncate()
    file.write(f'{os.getpid()}\n')
    file.flush()
    _file = file
    return True


def release():
    global _file
    if _file is None:
        return
    try:
        fcntl.flock(_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    _file.close()
    _file = None
