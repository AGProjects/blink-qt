"""One Blink per data directory.

Two instances on the same data directory overwrite each other's files: the SIP SIMPLE
configuration is kept in memory and written back whole, so one instance replaces the
accounts, address book and settings saved by the other (and a profile switch made by one
is undone by the other). The first instance holds an exclusive lock on <data>/lock for
its lifetime; a second one finds it taken and does not start.

The lock goes with the process: it is released when Blink exits or crashes, and before
the exec of a restart (the new image takes it again).

A second one started by hand asks the first to come to the front instead (a local socket named
after the data directory, listen() and ask_to_raise()): no warning, it just exits. The warning
is left for a first instance that does not answer (an older Blink, or one stuck).
"""

import os

from blink.resources import ApplicationData

try:
    import fcntl
except ImportError:         # Windows: no guard
    fcntl = None


__all__ = ['acquire', 'release', 'holder', 'listen', 'ask_to_raise']


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


# Bring the running instance to the front

_server = None


def _socket_name():
    import hashlib
    return 'blink-' + hashlib.sha1(os.path.realpath(ApplicationData.directory).encode()).hexdigest()[:16]


def listen(raise_window):
    """Called by the instance holding the lock: raise_window(activation token or None) when a second one asks."""
    global _server
    from PyQt6.QtNetwork import QLocalServer
    QLocalServer.removeServer(_socket_name())     # one left by a crashed instance (we hold the lock: it is not in use)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(_socket_name()):
        return False

    def connected():
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()

            def read(socket=socket):
                data = bytes(socket.readAll()).decode('utf-8', 'replace')
                if data.startswith('raise'):
                    token = data[len('raise'):].strip() or None
                    raise_window(token)
                    socket.write(b'ok')
                    socket.flush()
                socket.disconnectFromServer()
            socket.readyRead.connect(read)
            socket.disconnected.connect(socket.deleteLater)
    server.newConnection.connect(connected)
    _server = server
    return True


def ask_to_raise(timeout=2000):
    """Called by a second instance: True when the running one answered (it comes to the front). The launcher's
    activation token goes along, so that a Wayland compositor lets the window take the focus."""
    from PyQt6.QtNetwork import QLocalSocket
    socket = QLocalSocket()
    socket.connectToServer(_socket_name())
    if not socket.waitForConnected(timeout):
        return False
    token = os.environ.get('XDG_ACTIVATION_TOKEN') or os.environ.get('DESKTOP_STARTUP_ID') or ''
    socket.write(f'raise {token}'.encode())
    socket.flush()
    answered = socket.waitForReadyRead(timeout) and bytes(socket.readAll()) == b'ok'
    socket.disconnectFromServer()
    return answered
