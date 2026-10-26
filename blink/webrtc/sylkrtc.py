"""A client of the sylkRTC protocol: the WebSocket API of SylkServer's webrtcgateway (sylkRTC-2).

Requests are JSON objects with a sylkrtc name and a transaction id; the server
answers each with an ack or an error carrying the same transaction. Everything
else it sends is an event: ready-event once the connection is usable, then
account-event, session-event and videoroom-event.

One connection per wsServer URL (the server a domain publishes in its SylkServer
configuration), shared by the accounts on it. An account is added to a connection
(account-add) before it is used, but never registered: Blink registers over SIP
itself, a registration from the gateway would fork incoming calls and messages to
a second device.

Everything here runs in the GUI thread (QWebSocket).
"""

import json
import uuid

from hashlib import md5

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QNetworkRequest
try:
    from PyQt6.QtWebSockets import QWebSocket, QWebSocketProtocol
except ImportError:     # python3-pyqt6.qtwebsockets is not installed
    QWebSocket = QWebSocketProtocol = None

try:
    from PyQt6.QtWebSockets import QWebSocketHandshakeOptions     # Qt 6.4
except ImportError:
    QWebSocketHandshakeOptions = None

from application.python.types import Singleton

from sipsimple.configuration.settings import SIPSimpleSettings

from blink.logging import ActivityLog
from blink.webrtc import ws_server


__all__ = ['SylkRTCConnection', 'SylkRTCConnections', 'ws_server', 'websockets_available', 'known_features', 'probe_features', 'PROTOCOL']


PROTOCOL = 'sylkRTC-2'
PING_INTERVAL = 30 * 1000       # ms, a WebSocket ping keeps NATs and proxies from dropping the connection
CONNECT_TIMEOUT = 15 * 1000     # ms until ready-event
REQUEST_TIMEOUT = 20 * 1000     # ms without ack or error: the request failed
FEATURES_KEY = 'webrtc/gateway_features'    # {wsServer URL: [feature]}, what each gateway announced last


def websockets_available():
    return QWebSocket is not None


def _account_id(account):
    return '%s@%s' % (account.id.username, account.id.domain)


def _ha1(account):
    """What account-add takes as password: HA1 of the SIP credentials, as sylkrtc.js sends it."""
    username = getattr(account.auth, 'username', None) or account.id.username
    password = account.auth.password or ''
    return md5(f'{username}:{account.id.domain}:{password}'.encode()).hexdigest()


class SylkRTCConnection(QObject):
    """A WebSocket to one webrtcgateway.

    request(data, callback) sends a request once the connection is ready (queued
    until then) and calls callback(error) when it is answered: error is None for an
    ack, the server's reason otherwise. Events go out by the received signal.
    """

    ready = pyqtSignal()
    closed = pyqtSignal(str)        # the reason
    received = pyqtSignal(dict)     # an event; not 'event', which would shadow QObject.event()

    def __init__(self, url):
        super(SylkRTCConnection, self).__init__()
        self.url = url
        self.state = 'idle'         # idle, connecting, ready, closed
        self.accounts = set()       # account ids added
        self.features = set()       # what the gateway announced in its ready-event (sip-instance, multiple-video-feeds)
        self._pending_accounts = {} # account id -> callbacks waiting for account-add
        self._queue = []            # requests made before ready
        self._transactions = {}     # transaction -> (request name, callback, timer)
        self._close_reason = None

        self.socket = QWebSocket('', QWebSocketProtocol.Version.VersionLatest, self)
        self.socket.connected.connect(self._SH_Connected)
        self.socket.disconnected.connect(self._SH_Disconnected)
        self.socket.textMessageReceived.connect(self._SH_TextMessageReceived)
        self.socket.sslErrors.connect(self._SH_SslErrors)
        self.socket.errorOccurred.connect(self._SH_ErrorOccurred)

        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(PING_INTERVAL)
        self._ping_timer.timeout.connect(lambda: self.socket.ping())

        self._connect_timer = QTimer(self)
        self._connect_timer.setSingleShot(True)
        self._connect_timer.setInterval(CONNECT_TIMEOUT)
        self._connect_timer.timeout.connect(lambda: self._fail('no answer from the server'))

    def __repr__(self):
        return f'<SylkRTCConnection {self.url} {self.state}>'

    # Connection

    def open(self):
        if self.state in ('connecting', 'ready'):
            return
        self.state = 'connecting'
        self._close_reason = None
        ActivityLog().info(f'[sylkrtc] Connecting to {self.url}')
        request = QNetworkRequest(QUrl(self.url))
        user_agent = SIPSimpleSettings().user_agent
        if user_agent:
            request.setRawHeader(b'User-Agent', user_agent.encode())
        if QWebSocketHandshakeOptions is not None:
            options = QWebSocketHandshakeOptions()
            options.setSubprotocols([PROTOCOL])
            self.socket.open(request, options)
        else:
            request.setRawHeader(b'Sec-WebSocket-Protocol', PROTOCOL.encode())
            self.socket.open(request)
        self._connect_timer.start()

    def close(self, reason='closed'):
        if self.state in ('idle', 'closed'):
            return
        ActivityLog().info(f'[sylkrtc] Closing the connection to {self.url}')
        self._close_reason = reason
        self.socket.close()
        self._finish(reason)

    def _fail(self, reason):
        if self.state in ('idle', 'closed'):
            return
        ActivityLog().warning(f'[sylkrtc] Connection to {self.url} failed: {reason}')
        self._close_reason = reason
        self.socket.abort()
        self._finish(reason)

    def _finish(self, reason):
        if self.state == 'closed':
            return
        self.state = 'closed'
        self._ping_timer.stop()
        self._connect_timer.stop()
        self.accounts.clear()
        pending = [callback for callbacks in self._pending_accounts.values() for callback in callbacks]
        self._pending_accounts.clear()
        queued, self._queue = self._queue, []
        transactions, self._transactions = self._transactions, {}
        for name, callback, timer in transactions.values():
            timer.stop()
            if callback is not None:
                callback(reason)
        for data, callback in queued:
            if callback is not None:
                callback(reason)
        for callback in pending:
            callback(reason)
        self.closed.emit(reason)

    def _SH_Connected(self):
        protocol = self.socket.subprotocol() if hasattr(self.socket, 'subprotocol') else PROTOCOL
        ActivityLog().info(f'[sylkrtc] Connected to {self.url} (protocol {protocol or "none"}), waiting for the server')
        self._ping_timer.start()

    def _SH_Disconnected(self):
        reason = self._close_reason or self.socket.closeReason() or self.socket.errorString() or 'disconnected'
        if self.state != 'closed':
            ActivityLog().info(f'[sylkrtc] Disconnected from {self.url}: {reason}')
        self._finish(reason)

    def _SH_ErrorOccurred(self, error):
        if error == QAbstractSocket.SocketError.RemoteHostClosedError:
            return      # disconnected follows
        self._fail(self.socket.errorString())

    def _SH_SslErrors(self, errors):
        descriptions = ', '.join(error.errorString() for error in errors)
        try:
            verify = SIPSimpleSettings().tls.verify_server
        except AttributeError:
            verify = False
        if verify:
            ActivityLog().warning(f'[sylkrtc] TLS certificate of {self.url} refused: {descriptions}')
            return
        ActivityLog().info(f'[sylkrtc] TLS certificate of {self.url} not verified (verification is off): {descriptions}')
        self.socket.ignoreSslErrors()

    # Messages

    def _SH_TextMessageReceived(self, text):
        try:
            message = json.loads(text)
        except ValueError:
            ActivityLog().warning(f'[sylkrtc] Unreadable message from {self.url}: {text[:200]}')
            return
        kind = message.get('sylkrtc')
        if kind == 'ready-event':
            self._connect_timer.stop()
            self.state = 'ready'
            features = message.get('features')
            self.features = {feature for feature in features if isinstance(feature, str)} if isinstance(features, list) else set()
            _remember_features(self.url, self.features)
            ActivityLog().info(f'[sylkrtc] {self.url} is ready' + (f' ({", ".join(sorted(self.features))})' if self.features else ''))
            queued, self._queue = self._queue, []
            for data, callback in queued:
                self._send(data, callback)
            self.ready.emit()
        elif kind in ('ack', 'error'):
            transaction = message.get('transaction')
            name, callback, timer = self._transactions.pop(transaction, (None, None, None))
            if timer is not None:
                timer.stop()
            if kind == 'error':
                ActivityLog().warning(f'[sylkrtc] {name or "request"} failed: {message.get("error")}')
            if callback is not None:
                callback(message.get('error') or 'error' if kind == 'error' else None)
        elif kind:
            self.received.emit(message)

    def request(self, data, callback=None):
        data = dict(data)
        data['transaction'] = str(uuid.uuid4())
        if self.state == 'ready':
            self._send(data, callback)
        elif self.state == 'connecting':
            self._queue.append((data, callback))
        else:
            if callback is not None:
                callback('not connected')

    def _send(self, data, callback):
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(REQUEST_TIMEOUT)
        transaction = data['transaction']
        timer.timeout.connect(lambda: self._timeout(transaction))
        self._transactions[transaction] = (data.get('sylkrtc'), callback, timer)
        timer.start()
        self.socket.sendTextMessage(json.dumps(data))

    def _timeout(self, transaction):
        name, callback, timer = self._transactions.pop(transaction, (None, None, None))
        if name is None:
            return
        ActivityLog().warning(f'[sylkrtc] {name} timed out')
        if callback is not None:
            callback('timeout')

    # Accounts

    def add_account(self, account, callback):
        """account-add the SIP account (without registering it); callback(error) when done."""
        self.add_identity(_account_id(account), _ha1(account), account.display_name or '', callback)

    def add_identity(self, account_id, password, display_name, callback):
        """account-add an address (ours, or a guest one at another server) with its HA1; callback(error) when done."""
        if account_id in self.accounts:
            callback(None)
            return
        if account_id in self._pending_accounts:
            self._pending_accounts[account_id].append(callback)
            return
        self._pending_accounts[account_id] = [callback]

        def added(error):
            callbacks = self._pending_accounts.pop(account_id, [])
            if error is None:
                self.accounts.add(account_id)
                ActivityLog().info(f'[sylkrtc] Account {account_id} added to {self.url}')
            for function in callbacks:
                function(error)

        self.open()
        self.request(dict(sylkrtc='account-add',
                          account=account_id,
                          password=password,
                          display_name=display_name or '',
                          user_agent=SIPSimpleSettings().user_agent or 'Blink'), added)


class SylkRTCConnections(object, metaclass=Singleton):
    """The connections, by wsServer URL."""

    def __init__(self):
        self._connections = {}

    def get(self, url):
        connection = self._connections.get(url)
        if connection is None or connection.state == 'closed':
            connection = self._connections[url] = SylkRTCConnection(url)
        return connection

    def release(self, connection):
        """Nothing uses the connection any more: close it."""
        if self._connections.get(connection.url) is connection:
            del self._connections[connection.url]
        connection.close('not used')


# What a gateway can do (its ready-event features): remembered per server, asked again when it matters

def _saved_features():
    from blink.resources import ui_settings
    try:
        saved = json.loads(ui_settings().value(FEATURES_KEY, '') or '{}')
    except (TypeError, ValueError):
        saved = {}
    return saved if isinstance(saved, dict) else {}


def _remember_features(url, features):
    from blink.resources import ui_settings
    saved = _saved_features()
    if saved.get(url) != sorted(features):
        saved[url] = sorted(features)
        ui_settings().setValue(FEATURES_KEY, json.dumps(saved))


def known_features(url):
    """The features the gateway at url announced: from its connection when there is one, else as last seen; None when never seen."""
    connection = SylkRTCConnections()._connections.get(url)
    if connection is not None and connection.state == 'ready':
        return set(connection.features)
    features = _saved_features().get(url)
    return set(features) if isinstance(features, list) else None


_probes = set()


def probe_features(url, callback):
    """Ask the gateway at url what it can do: callback(features) once it is ready (now, if a connection is); nothing when it cannot be reached."""
    connection = SylkRTCConnections()._connections.get(url)
    if connection is not None and connection.state == 'ready':
        callback(set(connection.features))
        return
    if not websockets_available():
        return
    probe = SylkRTCConnection(url)      # one of its own, closed once answered, not one a room may come to use

    def ready():
        _probes.discard(probe)
        features = set(probe.features)
        probe.close('features known')
        callback(features)

    probe.ready.connect(ready)
    probe.closed.connect(lambda reason: _probes.discard(probe))
    _probes.add(probe)
    probe.open()
