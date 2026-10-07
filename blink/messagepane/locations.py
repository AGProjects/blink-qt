"""Location bubbles: OpenStreetMap tiles, a share's trail, and the map window.

A location message (category 'location': a one-shot position, the start of a
live share or of a meet-up) is drawn as a map with a pin where the position
is, the trail of a live share over it (its update ticks, filed against it by
related_msg_id) and, for a meet-up, the meeting point. LocationStore reads a
share from history in the db thread and decrypts what is armoured with the
account's key (each tick once per session); it reloads a share when a tick of
it is stored (BlinkMessageHistoryLocationDidStore) and says so (changed).

Tiles come from OpenStreetMap (as Blink for macOS and Sylk Mobile draw them),
with Blink's User-Agent as the tile usage policy asks, a few at a time, and are
kept on disk for good in ApplicationData 'map_tiles/z/x/y.png' (TileCache).

A click on the bubble opens LocationWindow: the same map, dragged to pan,
the wheel to zoom, Recentre to fit the trail again, a slider to go along the
trail (the pin follows, with the time of the point), Open in OpenStreetMap.
"""

import math
import os
import time

from datetime import datetime, timezone

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from PyQt6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget

from sipsimple.threading import run_in_thread

from blink.logging import ActivityLog, MessagingTrace as log
from blink.util import call_in_gui_thread, run_in_gui_thread, translate


__all__ = ['TileCache', 'LocationStore', 'paint_map', 'fit_view', 'LocationWindow']


TILE_SIZE = 256
DEFAULT_ZOOM = 15
MAX_ZOOM = 18
MIN_ZOOM = 2
TILE_HOST = '%s.tile.openstreetmap.de'
SUBDOMAINS = ('a', 'b', 'c')


def user_agent():
    try:
        from blink.__info__ import __version__
    except ImportError:
        __version__ = ''
    return f'Blink SIP client {__version__} (https://icanblink.com)'.replace('  ', ' ')


def world_point(latitude, longitude, zoom):
    """The position in pixels on the whole map at a zoom level (QPointF)."""
    n = 2.0 ** zoom * TILE_SIZE
    x = (longitude + 180.0) / 360.0 * n
    latitude = max(-85.05112878, min(85.05112878, latitude))
    y = (1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2.0 * n
    return QPointF(x, y)


def world_to_latlng(point, zoom):
    n = 2.0 ** zoom * TILE_SIZE
    longitude = point.x() / n * 360.0 - 180.0
    latitude = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * point.y() / n))))
    return latitude, longitude


# Tiles

@implementer(IObserver)
class TileCache(QObject):
    """Map tiles: memory, then disk, then the network (a few requests at a time)."""

    tileReady = pyqtSignal()

    concurrent = 4
    memory_limit = 400          # tiles kept as pixmaps
    timeout = 15                # seconds a tile may take
    retry_after = 60            # seconds before a tile that failed is asked for again

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        from collections import OrderedDict
        self._memory = OrderedDict()
        self._queue = []
        self._pending = set()
        self._failed = {}           # key: time.monotonic() it failed
        self._running = 0
        self._network = None
        self._directory = None

    def directory(self):
        if self._directory is None:
            from blink.resources import ApplicationData
            self._directory = ApplicationData.get('map_tiles')
        return self._directory

    def _path(self, zoom, x, y):
        return os.path.join(self.directory(), str(zoom), str(x), f'{y}.png')

    def tile(self, zoom, x, y):
        """The tile as a QPixmap, or None until it is here (tileReady is emitted then)."""
        key = (zoom, x, y)
        pixmap = self._memory.get(key)
        if pixmap is not None:
            self._memory.move_to_end(key)
            return pixmap
        path = self._path(zoom, x, y)
        if os.path.exists(path):
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self._remember(key, pixmap)
                return pixmap
        if key in self._failed and time.monotonic() - self._failed[key] > self.retry_after:
            del self._failed[key]
        if key not in self._pending and key not in self._failed:
            self._pending.add(key)
            self._queue.append(key)
            self._next()
        return None

    def _remember(self, key, pixmap):
        self._memory[key] = pixmap
        while len(self._memory) > self.memory_limit:
            self._memory.popitem(last=False)

    def _next(self):
        from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
        if self._network is None:
            self._network = QNetworkAccessManager(self)
        while self._queue and self._running < self.concurrent:
            key = self._queue.pop()             # the newest wanted first: what is on screen now
            zoom, x, y = key
            host = TILE_HOST % SUBDOMAINS[(x + y) % len(SUBDOMAINS)]
            request = QNetworkRequest(QUrl(f'https://{host}/{zoom}/{x}/{y}.png'))
            request.setRawHeader(b'User-Agent', user_agent().encode())
            request.setTransferTimeout(self.timeout * 1000)      # a stalled request must not hold a slot for ever
            reply = self._network.get(request)
            self._running += 1
            reply.finished.connect(lambda reply=reply, key=key: self._finished(reply, key))

    def _finished(self, reply, key):
        from PyQt6.QtNetwork import QNetworkReply
        self._running -= 1
        self._pending.discard(key)
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                if not self._failed:
                    ActivityLog().warning(f'[location] Map tiles cannot be fetched from {TILE_HOST % "*"}: {reply.errorString()} (asked again in {self.retry_after} s)')
                log.debug(f'Map tile {key} not fetched: {reply.errorString()}')
                self._failed[key] = time.monotonic()
                return
            data = bytes(reply.readAll())
            pixmap = QPixmap()
            if not pixmap.loadFromData(data):
                self._failed[key] = time.monotonic()
                return
            path = self._path(*key)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as tile_file:
                    tile_file.write(data)
            except OSError as e:
                log.warning(f'Cannot keep map tile {path}: {e}')
            self._remember(key, pixmap)
            self.tileReady.emit()
        finally:
            reply.deleteLater()
            self._next()


# Drawing

def fit_view(points, size, padding=28):
    """(zoom, centre in world pixels) showing all points (latitude, longitude) in size."""
    if not points:
        return DEFAULT_ZOOM, world_point(0, 0, DEFAULT_ZOOM)
    if len(points) == 1:
        zoom = DEFAULT_ZOOM
        return zoom, world_point(points[0][0], points[0][1], zoom)
    for zoom in range(17, MIN_ZOOM - 1, -1):
        projected = [world_point(lat, lng, zoom) for lat, lng in points]
        left, right = min(p.x() for p in projected), max(p.x() for p in projected)
        top, bottom = min(p.y() for p in projected), max(p.y() for p in projected)
        if right - left <= size.width() - 2 * padding and bottom - top <= size.height() - 2 * padding:
            return zoom, QPointF((left + right) / 2, (top + bottom) / 2)
    return MIN_ZOOM, world_point(points[-1][0], points[-1][1], MIN_ZOOM)


def paint_map(painter, rect, zoom, centre, track=(), pin=None, destination=None, start=None, accent=QColor('#1a73e8')):
    """Tiles of the area around centre (world pixels at zoom) in rect, then the trail and the pins."""
    tiles = TileCache.instance()
    n = 2 ** zoom
    left = centre.x() - rect.width() / 2
    top = centre.y() - rect.height() / 2
    painter.save()
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    painter.fillRect(rect, QColor('#e5e3df'))
    first_x, last_x = int(math.floor(left / TILE_SIZE)), int(math.floor((left + rect.width()) / TILE_SIZE))
    first_y, last_y = int(math.floor(top / TILE_SIZE)), int(math.floor((top + rect.height()) / TILE_SIZE))
    for tile_y in range(max(0, first_y), min(n - 1, last_y) + 1):
        for tile_x in range(first_x, last_x + 1):
            pixmap = tiles.tile(zoom, tile_x % n, tile_y)
            if pixmap is not None:
                painter.drawPixmap(QRectF(rect.left() + tile_x * TILE_SIZE - left, rect.top() + tile_y * TILE_SIZE - top, TILE_SIZE, TILE_SIZE), pixmap, QRectF(pixmap.rect()))

    def place(latlng):
        point = world_point(latlng[0], latlng[1], zoom)
        return QPointF(rect.left() + point.x() - left, rect.top() + point.y() - top)

    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if len(track) > 1:
        path = QPainterPath(place(track[0]))
        for point in track[1:]:
            path.lineTo(place(point))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(path)
        painter.setPen(QPen(accent, 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(path)
    if start is not None and len(track) > 1:
        painter.setPen(QPen(QColor('#ffffff'), 2))
        painter.setBrush(QColor('#3fb950'))
        painter.drawEllipse(place(start), 5, 5)
    if destination is not None:
        _paint_pin(painter, place(destination), QColor('#8e44ad'))
    if pin is not None:
        _paint_pin(painter, place(pin), QColor('#e53935'))
    painter.restore()


def _paint_pin(painter, tip, colour):
    """A map pin with its point on tip."""
    radius = 9
    head = QPointF(tip.x(), tip.y() - 2.2 * radius)
    path = QPainterPath()
    path.moveTo(tip)
    path.cubicTo(QPointF(tip.x() - radius * 0.4, tip.y() - radius * 0.9), QPointF(head.x() - radius, head.y() + radius * 0.6), QPointF(head.x() - radius, head.y()))
    path.arcTo(QRectF(head.x() - radius, head.y() - radius, 2 * radius, 2 * radius), 180, -180)
    path.cubicTo(QPointF(head.x() + radius, head.y() + radius * 0.6), QPointF(tip.x() + radius * 0.4, tip.y() - radius * 0.9), tip)
    painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
    painter.setBrush(colour)
    painter.drawPath(path)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('#ffffff'))
    painter.drawEllipse(head, radius * 0.38, radius * 0.38)


# What a share is

def _parse_time(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@implementer(IObserver)
class LocationStore(QObject):
    """The shares behind location bubbles, by message id, read from history."""

    changed = pyqtSignal(str)       # a message id whose share was (re)read

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._shares = {}           # message id: share dict, or False when it cannot be read
        self._versions = {}         # message id: how many times it was read
        self._pending = set()
        self._by_session = {}       # session id: message id of its bubble
        self._stale = {}            # message id: the previous reading, drawn while it is read again
        self._coords = {}           # tick message id: coordinates (db thread), decrypted once
        self._decrypted = {}        # armoured text: plaintext (db thread)
        NotificationCenter().add_observer(self, name='BlinkMessageHistoryLocationDidStore')

    def get(self, item):
        """The share of a location bubble ({...}), False when it cannot be read, None while it is read."""
        share = self._shares.get(item.id)
        if share is None and item.id not in self._pending:
            self._pending.add(item.id)
            self._by_session[item.related_msg_id or item.id] = item.id
            self._load(item.id, item.related_msg_id or item.id, item.account_id, item.content, item.content_type, item.metadata, item.related_action)
        return share

    def version(self, message_id):
        return self._versions.get(message_id, 0)

    @run_in_thread('db')
    def _load(self, message_id, session_id, account_id, content, content_type, metadata, related_action):
        from blink.history import Message, NOT_DELETED_SQL
        from blink.location import TEARDOWN_ACTIONS, append_track_point, location_payload, row_metadata
        try:
            db = Message._connection
            rows = list(Message.select(f'(message_id = {db.sqlrepr(message_id)} or related_msg_id = {db.sqlrepr(session_id)}) and {NOT_DELETED_SQL}', orderBy='timestamp'))
        except Exception as e:
            log.warning(f'Cannot read the location share {session_id}: {e!r}')
            rows = []
        origin = None
        track, ended, expires, destination, last_time = [], False, None, None, None
        for row in rows:
            action = row.related_action
            if action in TEARDOWN_ACTIONS:
                ended = True
                continue
            payload = self._payload(row, account_id, location_payload, row_metadata)
            if not payload:
                continue
            if row.message_id == message_id:
                origin = payload
                for point in payload['track']:
                    track = append_track_point(track, point)
            if payload.get('expires'):
                expires = _parse_time(payload['expires']) or expires
            coords = payload.get('coords')
            if coords:
                track = append_track_point(track, dict(coords, timestamp=coords.get('timestamp') or row.timestamp.replace(tzinfo=timezone.utc).isoformat()))
                last_time = row.timestamp.replace(tzinfo=timezone.utc)
                if coords.get('destination'):
                    destination = coords['destination']
        if origin is None:
            call_in_gui_thread(self._loaded, message_id, False)
            return
        action = origin['action']
        share = {
            'action': action,
            'kind': 'once' if origin.get('one_shot') or action == 'location_once' else 'meet' if action.startswith('meeting') else 'live',
            'track': [(point['latitude'], point['longitude'], _parse_time(point.get('timestamp'))) for point in track],
            'destination': (destination['latitude'], destination['longitude']) if destination else None,
            'ended': ended,
            'expires': expires,
            'updated': last_time,
            'accuracy': (origin.get('coords') or {}).get('accuracy'),
        }
        call_in_gui_thread(self._loaded, message_id, share)

    def _payload(self, row, account_id, location_payload, row_metadata):
        known = self._coords.get(row.message_id)

        def decrypt(text):
            return self._decrypt(row.account_id or account_id, text)

        try:
            payload = location_payload(row.content, row_metadata(row.metadata, row.related_action, row.related_msg_id), decrypt=decrypt, content_type=row.content_type)
        except Exception as e:
            log.debug(f'Location message {row.message_id} cannot be read: {e!r}')
            return None
        if payload and payload.get('coords'):
            self._coords[row.message_id] = payload['coords']
        elif payload and known is not None:
            payload['coords'] = known
        return payload

    def _decrypt(self, account_id, text):
        cached = self._decrypted.get(text)
        if cached is not None:
            return cached
        from blink.history import ConversationPreviews
        key = ConversationPreviews()._private_key(account_id)
        if key is None:
            return ''
        try:
            import pgpy
            plaintext = key.decrypt(pgpy.PGPMessage.from_blob(text)).message
        except Exception as e:
            log.debug(f'A location of {account_id} cannot be decrypted: {e!r}')
            return ''
        if isinstance(plaintext, (bytes, bytearray)):
            plaintext = bytes(plaintext).decode('utf-8', 'replace')
        if len(self._decrypted) > 5000:
            self._decrypted.clear()
        self._decrypted[text] = plaintext
        return plaintext

    def _loaded(self, message_id, share):
        self._pending.discard(message_id)
        self._shares[message_id] = share
        self._versions[message_id] = self._versions.get(message_id, 0) + 1
        self.changed.emit(message_id)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkMessageHistoryLocationDidStore(self, notification):
        message_id = self._by_session.get(notification.data.session_id)
        if message_id is not None and message_id in self._shares:
            # read again when it is next drawn, showing what it had meanwhile
            self._stale[message_id] = self._shares.pop(message_id)
            self.changed.emit(message_id)

    def shown(self, item):
        """What to draw now: the share, or while it is read again the previous reading."""
        share = self.get(item)
        if share is None:
            return self._stale.get(item.id)
        self._stale.pop(item.id, None)
        return share


def share_title(share):
    if share['kind'] == 'once':
        return translate('location', 'Location')
    if share['kind'] == 'meet':
        return translate('location', 'Meet-up')
    return translate('location', 'Live location')


def share_detail(share):
    parts = []
    now = datetime.now(timezone.utc)
    if share['kind'] != 'once':
        if share['ended']:
            parts.append(translate('location', 'ended'))
        elif share['expires'] is not None and share['expires'] <= now:
            parts.append(translate('location', 'expired'))
        elif share['expires'] is not None:
            parts.append(translate('location', 'until %s') % share['expires'].astimezone().strftime('%H:%M'))
        if len(share['track']) > 1:
            parts.append(translate('location', '%d points') % len(share['track']))
    if share['updated'] is not None and share['kind'] != 'once':
        parts.append(translate('location', 'updated %s') % share['updated'].astimezone().strftime('%H:%M'))
    if share['accuracy'] and share['kind'] == 'once':
        parts.append(translate('location', '± %d m') % round(share['accuracy']))
    return ' · '.join(parts)


# The map window

class MapView(QWidget):
    """A map of a share to pan (drag), zoom (wheel) and go along (position)."""

    def __init__(self, share, parent=None):
        super().__init__(parent)
        self.share = share
        self.position = len(share['track']) - 1     # the point the pin is on
        self.zoom, self.centre = DEFAULT_ZOOM, QPointF()
        self._drag = None
        self.setMinimumSize(520, 380)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        TileCache.instance().tileReady.connect(self.update)
        self._fitted = False

    def recentre(self):
        points = [(lat, lng) for lat, lng, _ in self.share['track']]
        if self.share['destination']:
            points.append(self.share['destination'])
        self.zoom, self.centre = fit_view(points, QSize(self.width(), self.height()), padding=40)
        self.update()

    def set_share(self, share):
        at_end = self.position >= len(self.share['track']) - 1
        self.share = share
        if at_end:
            self.position = len(share['track']) - 1
        self.update()

    def resizeEvent(self, event):
        if not self._fitted:
            self._fitted = True
            self.recentre()
        super().resizeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        track = [(lat, lng) for lat, lng, _ in self.share['track']]
        pin = track[self.position] if 0 <= self.position < len(track) else None
        paint_map(painter, QRectF(self.rect()), self.zoom, self.centre, track=track, pin=pin, destination=self.share['destination'],
                  start=track[0] if track else None)
        painter.setPen(QColor(0, 0, 0, 160))
        painter.drawText(QRectF(self.rect()).adjusted(0, 0, -6, -4), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, '© OpenStreetMap')

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (event.position(), QPointF(self.centre))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            start, centre = self._drag
            self.centre = centre - (event.position() - start)
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):
        step = 1 if event.angleDelta().y() > 0 else -1
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + step))
        if zoom == self.zoom:
            return
        # keep the point under the mouse where it is
        mouse = event.position()
        offset = mouse - QPointF(self.width() / 2, self.height() / 2)
        anchor = self.centre + offset
        factor = 2.0 ** (zoom - self.zoom)
        self.centre = anchor * factor - offset
        self.zoom = zoom
        self.update()


class LocationWindow(QDialog):
    _open = {}      # message id: window

    @classmethod
    def show_for(cls, item, peer, parent=None):
        window = cls._open.get(item.id)
        if window is None:
            share = LocationStore.instance().shown(item)
            if not share:
                return
            window = cls._open[item.id] = cls(item, share, peer, parent)
        window.show()
        window.raise_()
        window.activateWindow()

    def __init__(self, item, share, peer, parent=None):
        super().__init__(parent)
        self.item = item
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f'{share_title(share)} — {peer}' if peer else share_title(share))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.map = MapView(share, self)
        layout.addWidget(self.map, 1)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.valueChanged.connect(self._SH_Slider)
        layout.addWidget(self.slider)
        row = QHBoxLayout()
        self.point_label = QLabel(self)
        row.addWidget(self.point_label, 1)
        recentre = QPushButton(translate('location', 'Recentre'), self)
        recentre.clicked.connect(self.map.recentre)
        row.addWidget(recentre)
        osm = QPushButton(translate('location', 'Open in OpenStreetMap'), self)
        osm.clicked.connect(self._open_osm)
        row.addWidget(osm)
        layout.addLayout(row)
        self.resize(640, 540)
        self._update(share)
        LocationStore.instance().changed.connect(self._SH_Changed)
        self.finished.connect(lambda *args: self._open.pop(item.id, None))

    def _update(self, share):
        count = len(share['track'])
        at_end = self.slider.value() >= self.slider.maximum()
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, count - 1))
        if at_end:
            self.slider.setValue(max(0, count - 1))
        self.slider.blockSignals(False)
        self.slider.setVisible(count > 1)
        self.map.set_share(share)
        self.map.position = self.slider.value() if count > 1 else count - 1
        self._describe()

    def _describe(self):
        share = self.map.share
        track = share['track']
        position = self.map.position
        text = share_detail(share)
        if 0 <= position < len(track) and track[position][2] is not None:
            when = track[position][2].astimezone().strftime('%Y-%m-%d %H:%M:%S')
            text = (translate('location', 'Point %d of %d at %s') % (position + 1, len(track), when) if len(track) > 1 else when) + ('  ·  ' + text if text else '')
        self.point_label.setText(text)

    def _SH_Slider(self, value):
        self.map.position = value
        self.map.update()
        self._describe()

    def _SH_Changed(self, message_id):
        if message_id != self.item.id:
            return
        share = LocationStore.instance().shown(self.item)
        if share:
            self._update(share)

    def _open_osm(self):
        track = self.map.share['track']
        if not track:
            return
        latitude, longitude, _ = track[self.map.position if 0 <= self.map.position < len(track) else -1]
        from blink.location import maps_url
        QDesktopServices.openUrl(QUrl(maps_url(latitude, longitude)))
