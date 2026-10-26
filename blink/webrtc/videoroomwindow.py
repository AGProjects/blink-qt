"""The window of a video conference room joined over WebRTC (blink.webrtc.videoroom).

The participants are tiles sized to the window (1 or 2 in a row, then 2x2, 3x2, 4x2, the rest
in a carousel above the call bar); our own camera is among them, or floats over them as a
thumbnail when that keeps the grid even; the call bar at the bottom is the one of the video call window,
and fades out the same way when the mouse rests.
"""

from math import ceil
from time import monotonic

from PyQt6.QtCore import Qt, QEvent, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGraphicsOpacityEffect,
                             QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QRadioButton, QWidget)

from application.notification import IObserver, NotificationCenter
from zope.interface import implementer

from blink.logging import ActivityLog
from blink.util import call_in_gui_thread
from blink.videowindow import CallBar, StatusLabel, ICONS, draw_icon, RED, white
from blink.widgets.video import VideoSurface


__all__ = ['VideoroomWindow']


import blink.webrtc.sharedialog     # the share icon (ICONS['share'])
import blink.webrtc.speakerdialog   # the speakers icon (ICONS['speakers'])


def _icon_hand(painter):
    """An open hand, raised, in the call bar's 24x18 box."""
    color = painter.pen().color()
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    palm = QPainterPath()
    palm.addRoundedRect(QRectF(7.5, 8, 9.5, 8.5), 3.5, 3.5)
    for x, top in ((7.6, 3.2), (10.1, 1.6), (12.6, 2.0), (15.0, 3.6)):
        palm.addRoundedRect(QRectF(x, top, 2.1, 9), 1.05, 1.05)
    thumb = QPainterPath()
    thumb.addRoundedRect(QRectF(0, 0, 2.2, 6.5), 1.1, 1.1)
    painter.drawPath(palm.simplified())
    painter.translate(7.2, 9.4)
    painter.rotate(-40)
    painter.translate(-1.1, -5.6)
    painter.drawPath(thumb)
    painter.restore()


def _icon_more(painter):
    """Three dots."""
    color = painter.pen().color()
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for x in (6, 12, 18):
        painter.drawEllipse(QPointF(x, 9), 1.8, 1.8)
    painter.restore()


def _icon_screen(painter):
    """A monitor."""
    pen = painter.pen()
    screen = QPainterPath()
    screen.addRoundedRect(QRectF(3, 2, 18, 11.5), 1.8, 1.8)
    painter.strokePath(screen, pen)
    painter.drawLine(QPointF(12, 13.5), QPointF(12, 16.5))
    painter.drawLine(QPointF(8, 16.8), QPointF(16, 16.8))


ICONS.setdefault('screen', _icon_screen)
ICONS.setdefault('hand', _icon_hand)
ICONS.setdefault('more', _icon_more)
HAND_COLOR = QColor(255, 196, 0)
SPEAKING_COLOR = QColor(52, 199, 89)


def _paint_speaking(painter, rect, compact=False):
    """The border of a tile whose participant is speaking."""
    painter.setClipping(False)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    width = 2 if compact else 3
    painter.setPen(QPen(SPEAKING_COLOR, width))
    painter.drawRoundedRect(rect.adjusted(width / 2, width / 2, -width / 2, -width / 2), TILE_RADIUS, TILE_RADIUS)


TILE_GAP = 6
TILE_RADIUS = 8
GRID_MAX = 8                # tiles in the grid (4x2); the others go in the carousel
THREE_IN_A_ROW_ASPECT = 2.2         # 3 tiles: in a row when the window is at least this wide for its height
THREE_IN_A_COLUMN_ASPECT = 0.75     # and in a column when it is at most this (portrait)
TWO_IN_A_COLUMN_ASPECT = 1.0        # 2 tiles: one over the other when the window is at most this wide for its height
FLOATING_MARGIN = 12        # the floating thumbnail of our own video, from the grid's edges
FLOATING_MIN_W, FLOATING_MAX_W = 160, 320
CAROUSEL_THUMB_W = 80
CAROUSEL_THUMB_H = 45
CAROUSEL_PADDING = 4
COMPACT_TILE_HEIGHT = 60    # smaller (the carousel's): no name pill, the name is the tooltip
CAROUSEL_RADIUS = 12
CAROUSEL_GAP = 8            # between the carousel and the call bar
CAROUSEL_ARROW_W = 24
PICK_COLOR = QColor(10, 132, 255)   # the carousel tile picked to go into the grid, and the tiles it can replace
NAME_PILL_HEIGHT = 22
ENDED_LINGER = 2500         # ms the window stays to show why the room ended
CONTROLS_IDLE_TIME = 5000   # ms without the mouse moving before the call bar hides
# Video loss, from Sylk desktop and Sylk Mobile. An average of the received video bitrate below
# the threshold (desktop) degrades the tile: it keeps its place and shows the initial. Degraded
# for VIDEO_DEGRADE_GRACE (mobile) it is demoted to the carousel, unless the user put it in the
# grid. Back above the threshold it recovers, and a demoted tile returns to the grid. No video
# bytes at all for VIDEO_STALL_TIME asks the server once for the video again (mobile).
VIDEO_BITRATE_ALPHA = 0.4
VIDEO_BITRATE_THRESHOLD = 80000     # bit/s
VIDEO_WARMUP_TIME = 5               # s after the feed comes up before it can degrade
VIDEO_DEGRADE_GRACE = 30            # s degraded before the tile leaves the grid
VIDEO_STALL_TIME = 20               # s without any video bytes before asking for the video again


def room_title(room):
    """The room's display name when its contact has one, else Room <user part>."""
    try:
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(room)
        name = getattr(contact, 'name', None)
    except Exception:
        name = None
    user = room.partition('@')[0]
    if name and name.partition('@')[0] != user and name != room:
        return name
    return 'Room %s' % user


NAME_PILL_MARGIN = 8
CALL_BAR_SCALE = 0.75
CALL_BAR_LIFT = TILE_GAP + NAME_PILL_MARGIN + NAME_PILL_HEIGHT + 6      # the call bar's bottom, from the window's


def _label_avoid(widget):
    """What a name pill must not be under, in widget's coordinates: the call bar, and our floating
    thumbnail over the grid; a pill under one is moved above it."""
    window = widget.window()
    rects = []
    bar = getattr(window, 'call_bar', None)
    if bar is not None:
        top_left = widget.mapFromGlobal(window.mapToGlobal(bar.geometry().topLeft()))
        rects.append(QRectF(top_left.x(), top_left.y(), bar.width(), bar.height()).adjusted(-4, -NAME_PILL_MARGIN, 4, 0))
    grid = getattr(window, 'grid', None)
    if grid is not None and grid.floating and grid.local is not None and grid.local is not widget and grid.local is not widget.parent():
        top_left = widget.mapFromGlobal(grid.mapToGlobal(grid.local.geometry().topLeft()))
        rects.append(QRectF(top_left.x(), top_left.y(), grid.local.width(), grid.local.height()).adjusted(-4, -NAME_PILL_MARGIN, 4, 4))
    return rects


def _name_pill_font():
    font = QFont(QApplication.font())
    font.setPointSizeF(9)
    font.setWeight(QFont.Weight.Medium)
    return font


def _name_pill_rect(rect, text, muted, corner=('bottom', 'left'), avoid=None):
    """(where the name pill goes, the text as it fits): at the tile's corner (vertical: top/bottom,
    horizontal: left/center/right), lifted above anything of avoid it would be under."""
    metrics = QFontMetrics(_name_pill_font())
    icon_width = 20 if muted else 0
    text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()) - 24 - icon_width)
    width = metrics.horizontalAdvance(text) + 16 + icon_width
    vertical, horizontal = corner
    if horizontal == 'right':
        x = rect.right() - NAME_PILL_MARGIN - width
    elif horizontal == 'center':
        x = rect.center().x() - width / 2
    else:
        x = rect.left() + NAME_PILL_MARGIN
    y = rect.top() + NAME_PILL_MARGIN if vertical == 'top' else rect.bottom() - NAME_PILL_HEIGHT - NAME_PILL_MARGIN
    pill = QRectF(x, y, width, NAME_PILL_HEIGHT)
    for obstacle in avoid or []:
        if pill.intersects(obstacle):
            pill.moveBottom(obstacle.top())
            if pill.top() < rect.top() + NAME_PILL_MARGIN:
                pill.moveTop(rect.top() + NAME_PILL_MARGIN)     # a tile too short for both: at the top
    return pill, text


def _paint_name_pill(painter, rect, text, muted, corner=('bottom', 'left'), avoid=None):
    pill, text = _name_pill_rect(rect, text, muted, corner, avoid)
    icon_width = 20 if muted else 0
    font = _name_pill_font()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 140))
    painter.drawRoundedRect(pill, NAME_PILL_HEIGHT / 2, NAME_PILL_HEIGHT / 2)
    x = pill.left() + 8
    if muted:
        draw_icon(painter, 'mic.slash', QRectF(x - 2, pill.top() + 3, 20, 16), RED)
        x += icon_width
    painter.setFont(font)
    painter.setPen(white(0.95))
    painter.drawText(QRectF(x, pill.top(), pill.right() - x, pill.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


def _paint_placeholder(painter, rect, name, detail=None):
    """The initial in a circle, for a tile without a picture."""
    diameter = min(rect.width(), rect.height()) * 0.36
    circle = QRectF(rect.center().x() - diameter / 2, rect.center().y() - diameter / 2 - 6, diameter, diameter)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(70, 74, 82))
    painter.drawEllipse(circle)
    font = QFont(QApplication.font())
    font.setPixelSize(max(10, int(diameter * 0.45)))
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.setPen(white(0.9))
    initial = (name.strip()[:1] or '?').upper()
    painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, initial)
    if detail:
        small = QFont(QApplication.font())
        small.setPointSizeF(8.5)
        painter.setFont(small)
        painter.setPen(white(0.6))
        painter.drawText(QRectF(rect.left(), circle.bottom() + 6, rect.width(), 18), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, detail)


def _hand_rect(rect, corner, compact=False):
    """Where the raised hand badge goes: the corner beside the name pill's (the other side, same edge)."""
    size = 16 if compact else NAME_PILL_HEIGHT + 4
    margin = 3 if compact else NAME_PILL_MARGIN
    vertical, horizontal = ('top', 'left') if compact else corner
    x = rect.left() + margin if horizontal == 'right' else rect.right() - margin - size
    y = rect.top() + margin if vertical == 'top' else rect.bottom() - margin - size
    if compact:
        x = rect.left() + margin
    return QRectF(x, y, size, size)


def _paint_hand(painter, rect, corner, compact=False):
    badge = _hand_rect(rect, corner, compact)
    size = badge.width()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(HAND_COLOR)
    painter.drawEllipse(badge)
    draw_icon(painter, 'hand', badge.adjusted(size * 0.16, size * 0.2, -size * 0.16, -size * 0.14), QColor(40, 30, 0))


def _paint_stats(painter, rect, text):
    """The feed's codec and bitrate, a small pill at the top middle of the tile (while the mouse is on it)."""
    font = QFont(QApplication.font())
    font.setPointSizeF(8)
    metrics = QFontMetrics(font)
    pill = QRectF(0, 0, metrics.horizontalAdvance(text) + 14, 18)
    pill.moveCenter(QPointF(rect.center().x(), rect.top() + NAME_PILL_MARGIN + 9))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 150))
    painter.drawRoundedRect(pill, 9, 9)
    painter.setFont(font)
    painter.setPen(white(0.85))
    painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)


def _paint_replace_hint(painter, rect):
    font = QFont(QApplication.font())
    font.setPointSizeF(10)
    font.setWeight(QFont.Weight.DemiBold)
    metrics = QFontMetrics(font)
    text = 'Replace'
    pill = QRectF(0, 0, metrics.horizontalAdvance(text) + 24, 28)
    pill.moveCenter(rect.center())
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(PICK_COLOR)
    painter.drawRoundedRect(pill, 14, 14)
    painter.setFont(font)
    painter.setPen(white(1.0))
    painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)


class ParticipantTile(QWidget):
    """A remote participant: the latest decoded picture, filling the tile, or the initial."""

    def __init__(self, participant, parent):
        super(ParticipantTile, self).__init__(parent)
        self.participant = participant
        self.image = None
        self.frames = 0             # pictures received
        self.since = None           # when the feed was established
        self.bitrate = 0.0          # the average received video bitrate, bit/s
        self._sample = None         # (time, video bytes) of the previous sample
        self.last_bytes = None      # when video bytes last came
        self.refreshed = False      # the server was asked for the video again during this stall
        self.degraded = None        # since when too little video comes: the initial is shown in place
        self.demoted = False        # degraded too long: in the carousel, as audio only
        self.pinned = False         # put in the grid by the user: degraded, but never demoted
        self.placementChanged = lambda: None    # set by the window: the tile moves between the grid and the carousel
        self.refreshVideo = lambda: None        # set by the window: ask the server for the video again
        self.label_corner = ('bottom', 'left')     # where the name goes, set by the grid
        self.hand = False           # the participant's hand is raised
        self.picked = False         # in the carousel, chosen to go into the grid
        self.replaceable = False    # in the grid, while a carousel tile is picked: a click puts it here
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)     # as an avatar in the carousel the video shows around it
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        participant.frameReady.connect(self.set_image)
        participant.changed.connect(self.update)
        participant.speakingChanged.connect(self._SH_SpeakingChanged)

    def release(self):
        try:
            self.participant.frameReady.disconnect(self.set_image)
            self.participant.changed.disconnect(self.update)
            self.participant.speakingChanged.disconnect(self._SH_SpeakingChanged)
        except TypeError:
            pass

    def _SH_SpeakingChanged(self, speaking):
        self.update()

    def set_image(self, image):
        self.image = image
        self.frames += 1        # the info panel's fps
        self.update()

    def check_video(self, now):
        """Sample the video bitrate, once a second, and degrade, demote or recover the tile."""
        participant = self.participant
        if getattr(participant, 'extra_video', None) is not None:
            return      # our own extra video: its picture comes from here, not from a feed
        if participant.video is not True or participant.state != 'established':
            self.since = self._sample = self.last_bytes = None
            self.bitrate = 0.0
            return
        received = participant.video_bytes
        if self._sample is None:
            self.since = self.last_bytes = now
        else:
            then, before = self._sample
            if now > then:
                rate = (received - before) * 8 / (now - then)
                self.bitrate = VIDEO_BITRATE_ALPHA * rate + (1 - VIDEO_BITRATE_ALPHA) * self.bitrate
            if received > before:
                self.last_bytes = now
                self.refreshed = False
        self._sample = (now, received)
        if self.underMouse():
            self.update()       # the stats pill follows the bitrate
        if now - self.since < VIDEO_WARMUP_TIME:
            return
        if now - self.last_bytes > VIDEO_STALL_TIME and not self.refreshed:
            self.refreshed = True
            self.refreshVideo()
        uri = participant.uri
        if self.bitrate < VIDEO_BITRATE_THRESHOLD:
            if self.degraded is None:
                self.degraded = now
                ActivityLog().info(f'[videoroom] {uri} video lost ({self.bitrate / 1000:.0f} kbit/s)')
                self.update()
            elif not self.demoted and not self.pinned and now - self.degraded > VIDEO_DEGRADE_GRACE:
                self.demoted = True
                ActivityLog().info(f'[videoroom] {uri} has no video for {VIDEO_DEGRADE_GRACE} s, moved to the carousel')
                self.placementChanged()
        elif self.degraded is not None:
            self.degraded = None
            ActivityLog().info(f'[videoroom] {uri} video back ({self.bitrate / 1000:.0f} kbit/s)')
            self.update()
            if self.demoted:
                self.demoted = False
                self.placementChanged()

    def set_pick_state(self, picked=False, replaceable=False):
        if (picked, replaceable) != (self.picked, self.replaceable):
            self.picked, self.replaceable = picked, replaceable
            self.setCursor(Qt.CursorShape.PointingHandCursor if replaceable else Qt.CursorShape.ArrowCursor)
            self.update()

    def enterEvent(self, event):
        self.update()       # the Replace hint follows the mouse
        super(ParticipantTile, self).enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super(ParticipantTile, self).leaveEvent(event)

    @property
    def avatar(self):
        """In the carousel without video (audio only, or demoted): a round avatar, nothing around it."""
        return not self.in_grid

    def _paint_avatar(self, painter, rect):
        from blink.avatars import avatar_color, avatar_initials
        name = self.participant.name
        diameter = min(rect.width(), rect.height()) - 4
        circle = QRectF(0, 0, diameter, diameter)
        circle.moveCenter(rect.center())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(avatar_color(self.participant.uri or name))
        painter.drawEllipse(circle)
        font = QFont(QApplication.font())
        font.setPixelSize(max(9, int(diameter * 0.38)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(white(0.95))
        painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, avatar_initials(name))
        if self.participant.speaking:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(SPEAKING_COLOR, 2))
            painter.drawEllipse(circle.adjusted(-1, -1, 1, 1))
        if self.participant.muted:
            badge = QRectF(circle.right() - 13, circle.bottom() - 13, 14, 14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawEllipse(badge)
            draw_icon(painter, 'mic.slash', badge.adjusted(2, 3, -2, -2), RED)
        if self.hand:
            badge = QRectF(circle.left() - 1, circle.top() - 1, 14, 14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(HAND_COLOR)
            painter.drawEllipse(badge)
            draw_icon(painter, 'hand', badge.adjusted(2, 2.5, -2, -2), QColor(40, 30, 0))

    @property
    def in_grid(self):
        """Audio only participants, and those demoted after losing video, are not in the grid, only in the carousel."""
        return self.participant.video is not False and not self.demoted

    def _detail(self):
        participant = self.participant
        if participant.video is False or self.demoted:
            return 'audio'
        if self.degraded is not None:
            return 'no video'
        if participant.state in ('attaching', 'connecting'):
            return 'connecting…'
        if participant.state == 'failed':
            return 'no media'
        return None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect())
        compact = rect.height() < COMPACT_TILE_HEIGHT
        if compact and self.avatar:
            self._paint_avatar(painter, rect)
            return
        clip = QPainterPath()
        clip.addRoundedRect(rect, TILE_RADIUS, TILE_RADIUS)
        painter.fillRect(rect, QColor('#000000'))
        painter.setClipPath(clip)
        painter.fillRect(rect, QColor(32, 34, 38))
        image = self.image
        # degraded in the grid: the initial in place of a frozen or black picture; in the
        # carousel the last picture stays, a clue of who it is
        if self.degraded is not None and not self.demoted:
            image = None
        if image is not None and not image.isNull():
            scale = max(rect.width() / image.width(), rect.height() / image.height())     # cropped to fill the tile
            width, height = image.width() * scale, image.height() * scale
            target = QRectF(rect.left() + (rect.width() - width) / 2, rect.top() + (rect.height() - height) / 2, width, height)
            painter.drawImage(target, image)
        else:
            _paint_placeholder(painter, rect, self.participant.name, None if compact else self._detail())
        if not compact:
            _paint_name_pill(painter, rect, self.participant.name, self.participant.muted, self.label_corner, _label_avoid(self))
        elif self.participant.muted:
            draw_icon(painter, 'mic.slash', QRectF(rect.right() - 18, rect.top() + 3, 15, 12), RED)
        if self.hand:
            _paint_hand(painter, rect, self.label_corner, compact)
        if self.participant.speaking and not (self.picked or self.replaceable):
            _paint_speaking(painter, rect, compact)
        if not compact and self.underMouse() and not self.replaceable and self.participant.video is True:
            parts = [self.participant.video_codec or '', '%d kbit/s' % round(self.bitrate / 1000) if self._sample is not None else '']
            text = ' · '.join(part for part in parts if part)
            if text:
                _paint_stats(painter, rect, text)
        if self.picked or self.replaceable:
            hovered = self.underMouse()
            painter.setClipping(False)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(PICK_COLOR, (2 if compact else 3) if self.picked or hovered else 1.5))
            painter.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), TILE_RADIUS, TILE_RADIUS)
            if self.replaceable and hovered:
                _paint_replace_hint(painter, rect)


class _ThumbnailButton(QAbstractButton):
    """On our own tile, under the mouse: out of the grid into the floating thumbnail, or back."""

    size = 26

    def __init__(self, tile):
        super(_ThumbnailButton, self).__init__(tile)
        self.floating = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.resize(self.size, self.size)
        self.hide()

    def set_floating(self, floating):
        self.floating = floating
        self.setToolTip('Put me back in the grid' if floating else 'Show me as a thumbnail')
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190 if self.underMouse() else 140))
        painter.drawEllipse(rect)
        # a frame with a small picture in a corner (the thumbnail), or filling it (the grid)
        painter.setPen(QPen(white(0.92), 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        frame = rect.adjusted(6.5, 8.5, -6.5, -8.5)
        painter.drawRoundedRect(frame, 1.5, 1.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(white(0.92))
        if self.floating:
            painter.drawRoundedRect(frame.adjusted(1.5, 1.5, -1.5, -1.5), 1, 1)
        else:
            painter.drawRoundedRect(QRectF(frame.right() - 6, frame.bottom() - 4.5, 5, 3.5), 0.8, 0.8)


class LocalTile(QWidget):
    """Our own camera, mirrored, as the other tiles."""

    def __init__(self, name, parent):
        super(LocalTile, self).__init__(parent)
        self.name = name
        self.muted = False
        self.camera_off = False
        self.label_corner = ('bottom', 'left')
        self.hand = False
        self.speaking = False
        self.blurred = False        # the picture comes from blink.webrtc.background_blur
        self.surface = VideoSurface(self, framerate=None)
        self.surface.mirror = True
        self.surface.crop = True
        self.surface.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.overlay = _LocalOverlay(self)
        self.thumbnail_button = _ThumbnailButton(self)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.attach_camera()

    def attach_camera(self):
        from sipsimple.application import SIPApplication
        try:
            producer = SIPApplication.video_device.producer
        except AttributeError:
            producer = None
        from blink.webrtc.videoroom import VideoroomSession
        screen = VideoroomSession.sending_screen_device()
        self.surface.mirror = not screen        # a shared screen shows as others see it, whole
        self.surface.crop = not screen
        from blink.webrtc.background_blur import BlurredCamera, blur_enabled
        blurred = blur_enabled() and not screen and not self.camera_off and producer is not None
        if blurred != self.blurred:
            self.blurred = blurred
            if blurred:
                self.surface.producer = None
                BlurredCamera().add_listener(self, self.surface.set_frame)     # as the others see us
            else:
                BlurredCamera().remove_listener(self)
                self.surface.clear_frame()
        elif blurred:
            BlurredCamera().refresh()       # the camera was switched
        try:
            if not blurred:
                self.surface.producer = None if self.camera_off else producer
        except Exception as e:
            ActivityLog().warning(f'[videoroom] Cannot show the camera: {e}')
        self.surface.setVisible(not self.camera_off and producer is not None)
        self.overlay.update()

    def release(self):
        if self.blurred:
            from blink.webrtc.background_blur import BlurredCamera
            self.blurred = False
            BlurredCamera().remove_listener(self)
        self.surface.stop()

    def set_camera_off(self, off):
        self.camera_off = off
        self.attach_camera()

    def set_muted(self, muted):
        self.muted = muted
        self.overlay.update()
        self.place_thumbnail_button()       # the pill is wider with the muted microphone

    def place_thumbnail_button(self):
        """In the first corner clear of the name pill and the raised hand badge, as they are painted:
        the right on the edge away from them first, then the others."""
        margin = 6
        button = self.thumbnail_button
        rect = QRectF(self.rect())
        taken = [_name_pill_rect(rect, 'Me', self.muted, self.label_corner, _label_avoid(self.overlay))[0].adjusted(-4, -4, 4, 4),
                 _hand_rect(rect, self.label_corner).adjusted(-4, -4, 4, 4)]
        away = 'bottom' if self.label_corner[0] == 'top' else 'top'
        near = 'top' if away == 'bottom' else 'bottom'
        w, h = button.width(), button.height()
        spots = []
        for vertical, horizontal in ((away, 'right'), (away, 'left'), (near, 'right'), (near, 'left')):
            x = rect.right() - margin - w if horizontal == 'right' else rect.left() + margin
            y = rect.bottom() - margin - h if vertical == 'bottom' else rect.top() + margin
            spots.append(QRectF(x, y, w, h))
        spot = next((spot for spot in spots if not any(spot.intersects(other) for other in taken)), spots[0])
        button.move(int(spot.x()), int(spot.y()))
        button.raise_()

    def enterEvent(self, event):
        self.place_thumbnail_button()       # where the name pill is now (it moves around the call bar and the thumbnail)
        self.thumbnail_button.setVisible(self.thumbnail_button.isEnabled())
        super(LocalTile, self).enterEvent(event)

    def leaveEvent(self, event):
        self.thumbnail_button.hide()
        super(LocalTile, self).leaveEvent(event)

    def resizeEvent(self, event):
        self.surface.setGeometry(self.rect())
        self.overlay.setGeometry(self.rect())
        self.overlay.raise_()
        self.place_thumbnail_button()
        super(LocalTile, self).resizeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.fillRect(rect, QColor('#000000'))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(32, 34, 38))
        painter.drawRoundedRect(rect, TILE_RADIUS, TILE_RADIUS)
        if not self.surface.isVisible():
            _paint_placeholder(painter, rect, self.name, 'camera off' if self.camera_off else 'no camera')


class _LocalOverlay(QWidget):
    def __init__(self, tile):
        super(_LocalOverlay, self).__init__(tile)
        self.tile = tile
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        # the corners of the picture, rounded as the other tiles
        corners = QPainterPath()
        corners.addRect(rect)
        rounded = QPainterPath()
        rounded.addRoundedRect(rect, TILE_RADIUS, TILE_RADIUS)
        painter.fillPath(corners.subtracted(rounded), QColor('#000000'))
        if self.tile.hand:
            _paint_hand(painter, rect, self.tile.label_corner, rect.height() < COMPACT_TILE_HEIGHT)
        if self.tile.speaking:
            _paint_speaking(painter, rect, rect.height() < COMPACT_TILE_HEIGHT)
        if rect.height() < COMPACT_TILE_HEIGHT:
            return      # in the carousel: the name is the tooltip
        _paint_name_pill(painter, rect, 'Me', self.tile.muted, self.tile.label_corner, _label_avoid(self))


class TileGrid(QWidget):
    """The tiles: 1 or 2 in a row, 2x2, 3x2, 4x2; our own video floating when that keeps the grid even; the rest in the carousel."""

    def __init__(self, parent):
        super(TileGrid, self).__init__(parent)
        self.tiles = []
        self.carousel = None    # takes the tiles past GRID_MAX
        self.speakers = []      # the room's speakers' tiles: alone in the grid, the others in the carousel
        self.local = None       # our own tile: in the grid, or floating over it as a thumbnail
        self.hide_local = False  # while the screen is shared
        self.self_hidden = False # we asked not to see ourselves (the others still do)
        self.local_placement = None  # None: as the layout has it; 'floating' or 'grid': as we asked (the tile's button)
        self.floating = False
        self.floating_corner = ('bottom', 'right')     # where the thumbnail is, moved by dragging it

    def add(self, tile):
        self.tiles.append(tile)
        tile.show()
        self.layout_tiles()

    def remove(self, tile):
        if tile in self.tiles:
            self.tiles.remove(tile)
            tile.hide()
            tile.deleteLater()
            self.layout_tiles()

    def swap(self, carousel_tile, grid_tile):
        """The carousel tile (one with video) takes the place of the grid one, which goes to the carousel where the other was."""
        a, b = self.tiles.index(carousel_tile), self.tiles.index(grid_tile)
        self.tiles[a], self.tiles[b] = self.tiles[b], self.tiles[a]
        self.layout_tiles()

    def resizeEvent(self, event):
        self.layout_tiles()
        super(TileGrid, self).resizeEvent(event)

    def layout_tiles(self):
        # hidden at our request only while there is someone else's video to show instead
        others = any(tile is not self.local and getattr(tile, 'in_grid', True) for tile in self.tiles)
        hidden = self.local if (self.hide_local or (self.self_hidden and others)) and self.local in self.tiles else None
        if hidden is not None:
            hidden.hide()       # sharing the screen: our own picture would only show the screen back
        elif self.local is not None and self.local in self.tiles and self.local.isHidden():
            self.local.show()
        tiles = [tile for tile in self.tiles if tile is not hidden]
        speakers = [tile for tile in self.speakers if tile in tiles]
        others = [tile for tile in tiles if tile not in speakers]
        video = [tile for tile in others if getattr(tile, 'in_grid', True)]
        audio = [tile for tile in others if not getattr(tile, 'in_grid', True)]
        local = self.local if self.local in tiles else None
        floating = False
        if self.carousel is None:
            shown, overflow = speakers + video, []
        elif speakers:
            shown, overflow = speakers, video + audio      # the speakers fill the window
        else:
            # our own video floats when that leaves an even grid of the others (3 of us: 2x1 and
            # the thumbnail, 5: 2x2 and the thumbnail, ...), or when the others fill the grid
            remotes = [tile for tile in video if tile is not local]
            people = len(remotes) + (1 if local is not None else 0)
            if self.local_placement == 'floating':
                floating = local is not None and len(remotes) >= 1     # with one other: them full, us the thumbnail
            elif self.local_placement == 'grid':
                floating = local is not None and len(remotes) >= GRID_MAX
            else:
                floating = local is not None and ((people >= 3 and people % 2 == 1) or len(remotes) >= GRID_MAX)
            if floating:
                shown, overflow = remotes[:GRID_MAX], remotes[GRID_MAX:] + audio
            else:
                shown, overflow = video[:GRID_MAX], video[GRID_MAX:] + audio
        self.floating = floating
        if local is not None:
            button = local.thumbnail_button
            button.set_floating(floating)
            # the choice only means something with others in the grid (and not with the room's speakers)
            button.setEnabled(not speakers and len([tile for tile in video if tile is not local]) >= 1)
            if not button.isEnabled():
                button.hide()
        if floating and local.parent() is not self:
            local.setParent(self)
            local.show()
        for tile in shown:
            if tile.parent() is not self:
                tile.setParent(self)
                tile.setToolTip('')
                tile.show()
        if self.carousel is not None:
            self.carousel.set_tiles(overflow)
        count = len(shown)
        if not count:
            return
        width, height = self.width() - 2 * TILE_GAP, self.height() - 2 * TILE_GAP
        if count == 1:
            columns, rows = 1, 1
        elif count == 2:
            # side by side, or one over the other in a portrait window
            columns, rows = (1, 2) if width / max(1, height) <= TWO_IN_A_COLUMN_ASPECT else (2, 1)
        elif count == 3:
            # 3x1 in a wide window, 1x3 in a tall (portrait) one, 2 over 1 otherwise
            aspect = width / max(1, height)
            if aspect >= THREE_IN_A_ROW_ASPECT:
                columns, rows = 3, 1
            elif aspect <= THREE_IN_A_COLUMN_ASPECT:
                columns, rows = 1, 3
            else:
                columns, rows = 2, 2
        else:
            columns, rows = int(ceil(count / 2)), 2    # 2x2, 3x2, 4x2 (a shorter bottom row centered)
        cell_w = (width - (columns - 1) * TILE_GAP) / columns
        cell_h = (height - (rows - 1) * TILE_GAP) / rows
        # tiles take the whole cell: the pictures are cropped to fill them
        for index, tile in enumerate(shown):
            row, column = divmod(index, columns)
            in_row = min(columns, count - row * columns)
            offset = (width - in_row * cell_w - (in_row - 1) * TILE_GAP) / 2      # a last, shorter row is centered
            x = TILE_GAP + offset + column * (cell_w + TILE_GAP)
            y = TILE_GAP + row * (cell_h + TILE_GAP)
            # the names at the outer corners: top for the top row, bottom for the others, left or right
            # by side (a middle column centered); one under the call bar is lifted above it when painted
            vertical = 'top' if row == 0 else 'bottom'
            if in_row % 2 and column == in_row // 2 and in_row > 1:
                horizontal = 'center'
            else:
                horizontal = 'left' if column < in_row / 2 else 'right'
            corner = (vertical, horizontal)
            if getattr(tile, 'label_corner', None) != corner:
                tile.label_corner = corner
                tile.update()
                overlay = getattr(tile, 'overlay', None)
                if overlay is not None:
                    overlay.update()
                if hasattr(tile, 'place_thumbnail_button'):
                    tile.place_thumbnail_button()
            tile.setGeometry(int(x), int(y), int(cell_w), int(cell_h))
        if floating:
            self.place_floating()

    @property
    def can_hide_self(self):
        return any(tile is not self.local and getattr(tile, 'in_grid', True) for tile in self.tiles)

    def set_self_hidden(self, hidden):
        self.self_hidden = hidden
        self.layout_tiles()

    def toggle_local_placement(self):
        self.local_placement = 'grid' if self.floating else 'floating'
        self.layout_tiles()

    def floating_rect(self, corner=None):
        vertical, horizontal = corner or self.floating_corner
        width = max(FLOATING_MIN_W, min(FLOATING_MAX_W, int(self.width() * 0.2)))
        height = int(width * 9 / 16)
        x = FLOATING_MARGIN if horizontal == 'left' else self.width() - FLOATING_MARGIN - width
        y = FLOATING_MARGIN if vertical == 'top' else self.height() - FLOATING_MARGIN - height
        return QRect(x, y, width, height)

    def place_floating(self):
        local = self.local
        local.label_corner = ('bottom', 'left')
        local.place_thumbnail_button()
        local.setGeometry(self.floating_rect())
        local.raise_()
        local.overlay.update()

    def snap_floating(self, center):
        """After a drag: the thumbnail goes to the corner nearest to where it was let go."""
        vertical = 'top' if center.y() < self.height() / 2 else 'bottom'
        horizontal = 'left' if center.x() < self.width() / 2 else 'right'
        self.floating_corner = (vertical, horizontal)
        self.place_floating()
        for tile in self.tiles:
            tile.update()       # name pills that went around it

class _CarouselArrow(QAbstractButton):
    """The caret at either end of the carousel when it can scroll that way, as in Sylk desktop."""

    def __init__(self, direction, parent):
        super(_CarouselArrow, self).__init__(parent)
        self.direction = direction      # -1 left, +1 right
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setToolTip('More participants')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QRectF(self.rect()).center()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.drawEllipse(c, 11, 11)
        painter.setBrush(white(1.0 if self.underMouse() else 0.8))
        w, h = 7, 12
        if self.direction < 0:
            points = [QPointF(c.x() + w / 2, c.y() - h / 2), QPointF(c.x() - w / 2, c.y()), QPointF(c.x() + w / 2, c.y() + h / 2)]
        else:
            points = [QPointF(c.x() - w / 2, c.y() - h / 2), QPointF(c.x() + w / 2, c.y()), QPointF(c.x() - w / 2, c.y() + h / 2)]
        painter.drawPolygon(QPolygonF(points))


class Carousel(QWidget):
    """The tiles past GRID_MAX, as thumbnails in a strip above the call bar.

    It shows and fades with the call bar. The wheel or the carets at its ends
    scroll it. A click on a thumbnail picks it, and a click on a tile in the
    grid then puts it there, the grid one taking its place in the carousel.
    """

    def __init__(self, parent):
        super(Carousel, self).__init__(parent)
        self.tiles = []
        self.offset = 0
        self.viewport = QWidget(self)       # clips the strip inside the padding
        self.content = QWidget(self.viewport)
        self.left_arrow = _CarouselArrow(-1, self)
        self.right_arrow = _CarouselArrow(+1, self)
        self.left_arrow.clicked.connect(lambda: self.scroll_by(-(CAROUSEL_THUMB_W + TILE_GAP)))
        self.right_arrow.clicked.connect(lambda: self.scroll_by(CAROUSEL_THUMB_W + TILE_GAP))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        for widget in (self, self.viewport, self.content):
            widget.setMouseTracking(True)
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self.opacity_effect)
        self.fade_animation = QPropertyAnimation(self.opacity_effect, b'opacity', self)
        self.fade_animation.finished.connect(self._SH_FadeFinished)
        self.shown = True           # follows the call bar: shown and hidden with it
        self.hide()

    def set_tiles(self, tiles):
        for tile in tiles:
            if tile.parent() is not self.content:
                tile.setParent(self.content)
                tile.show()
            tile.setToolTip(tile.participant.name if hasattr(tile, 'participant') else 'Me')
        self.tiles = list(tiles)
        x = 0
        for tile in self.tiles:
            width = self._width(tile)
            tile.setGeometry(x, 0, width, CAROUSEL_THUMB_H)
            tile.update()
            x += width + TILE_GAP
        self.setVisible(bool(self.tiles) and self.shown)
        self.tile()

    @staticmethod
    def _width(tile):
        return CAROUSEL_THUMB_H if getattr(tile, 'avatar', False) else CAROUSEL_THUMB_W     # an avatar is round

    @property
    def content_width(self):
        return sum(self._width(tile) + TILE_GAP for tile in self.tiles) - TILE_GAP if self.tiles else 0

    def tile(self):
        window = self.parent()
        height = CAROUSEL_THUMB_H + 2 * CAROUSEL_PADDING
        available = window.width() - 2 * TILE_GAP
        scrolls = self.content_width + 2 * CAROUSEL_PADDING > available
        inset = CAROUSEL_ARROW_W if scrolls else CAROUSEL_PADDING
        width = available if scrolls else self.content_width + 2 * CAROUSEL_PADDING
        self.setGeometry((window.width() - width) // 2, window.call_bar.y() - CAROUSEL_GAP - height, width, height)
        self.viewport.setGeometry(inset, CAROUSEL_PADDING, max(0, width - 2 * inset), CAROUSEL_THUMB_H)
        self.left_arrow.setGeometry(0, 0, CAROUSEL_ARROW_W, height)
        self.right_arrow.setGeometry(width - CAROUSEL_ARROW_W, 0, CAROUSEL_ARROW_W, height)
        self.scroll_to(self.offset)
        self.raise_()

    def scroll_by(self, delta):
        self.scroll_to(self.offset + delta)

    def scroll_to(self, offset):
        limit = max(0, self.content_width - self.viewport.width())
        self.offset = max(0, min(offset, limit))
        self.content.setGeometry(-self.offset, 0, self.content_width, CAROUSEL_THUMB_H)
        self.left_arrow.setVisible(self.offset > 0)
        self.right_arrow.setVisible(self.offset < limit)

    def wheelEvent(self, event):
        delta = event.angleDelta()
        self.scroll_by(-(delta.x() or delta.y()))
        event.accept()

    def fade_in(self):
        self._fade(1.0, 180)

    def fade_out(self):
        self._fade(0.0, 250)

    def _fade(self, value, duration):
        self.shown = value > 0
        if self.shown and self.tiles:
            self.show()
        self.fade_animation.stop()
        self.fade_animation.setDuration(duration)
        self.fade_animation.setStartValue(self.opacity_effect.opacity())
        self.fade_animation.setEndValue(value)
        self.fade_animation.start()

    def _SH_FadeFinished(self):
        if not self.shown:
            self.hide()

    # no background: the thumbnails and avatars float over the video

@implementer(IObserver)
class _CameraObserver(object):
    """The video device changing (the screen shared, or back to the camera) for a video room window."""

    def __init__(self, window):
        import weakref
        self.window = weakref.ref(window)
        NotificationCenter().add_observer(self, name='VideoDeviceDidChangeCamera')

    def close(self):
        NotificationCenter().discard_observer(self, name='VideoDeviceDidChangeCamera')

    def handle_notification(self, notification):
        window = self.window()
        if window is not None:
            call_in_gui_thread(window._camera_device_changed)


class VideoroomWindow(QWidget):
    windows = {}        # session -> window

    @classmethod
    def for_session(cls, session):
        window = cls.windows.get(session)
        if window is None:
            window = cls.windows[session] = cls(session)
        return window

    def __init__(self, session):
        super(VideoroomWindow, self).__init__(None)
        self.session = session
        self.tiles = {}             # participant -> tile
        self.duration = None        # seconds the room existed at join
        self.joined_at = None
        self._closing = False

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 240)
        self.resize(960, 600)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor('#000000'))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.title = room_title(session.room)
        self.setWindowTitle(self.title)

        self.grid = TileGrid(self)
        self.grid.setMouseTracking(True)
        self.grid.installEventFilter(self)
        display_name = getattr(session.account, 'display_name', None) or str(session.account.id)
        self.local_tile = LocalTile(display_name, self.grid)
        self._track_mouse(self.local_tile)
        self.grid.local = self.local_tile
        self.local_tile.thumbnail_button.clicked.connect(self.grid.toggle_local_placement)
        self._track_mouse(self.local_tile.thumbnail_button)
        self.grid.add(self.local_tile)
        self._drag = None           # (offset of the press in the floating thumbnail) while dragging it

        self.status_label = StatusLabel(self)
        self.status_label.set_status('Connecting…')

        self.call_bar = CallBar(self)
        self.call_bar.scale = CALL_BAR_SCALE
        self.call_bar.bottom = CALL_BAR_LIFT        # above the names at the bottom of the tiles: they stay visible
        self.mute_button = self.call_bar.add_segment('mic', 'Mute')
        self.camera_button = self.call_bar.add_segment('camera', 'Camera')
        self.camera_button.setToolTip('The camera to send, the screen, or none')
        self.speakers_button = self.call_bar.add_segment('speakers', 'Speakers')
        self.speakers_button.setToolTip('Speaker selection: the room\'s layout, for everyone')
        self.speakers_button.clicked.connect(self._SH_SpeakersButtonClicked)
        self.speakers_button.hide()
        self.chat_button = self.call_bar.add_segment('chat', 'Chat')
        self.chat_button.setToolTip('The room\'s chat, in the MSRP window')
        self.chat_button.clicked.connect(self._SH_ChatButtonClicked)
        self.stop_screen_button = self.call_bar.add_segment('screen', 'Stop sharing', destructive=True)
        self.stop_screen_button.setToolTip('Stop sharing the screen: the camera comes back')
        self.stop_screen_button.clicked.connect(self._stop_screen)
        self.stop_screen_button.hide()
        self.hand_button = self.call_bar.add_segment('hand', 'Raise hand')
        self.hand_button.clicked.connect(lambda: self.session.toggle_hand())
        self.hand_button.hide()     # with more than two of us (_update_speakers_button)
        self.more_button = self.call_bar.add_segment('more', 'More')
        self.more_button.clicked.connect(self._SH_MoreButtonClicked)
        self.fullscreen_button = self.call_bar.add_segment('fullscreen', 'Full screen')
        self.hangup_button = self.call_bar.add_segment('hangup', 'Leave', destructive=True)
        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        self.camera_button.clicked.connect(self._SH_CameraMenuClicked)
        self.fullscreen_button.clicked.connect(self.toggle_full_screen)
        self.hangup_button.clicked.connect(self._SH_HangupButtonClicked)

        self.carousel = Carousel(self)
        for widget in (self.carousel, self.carousel.viewport, self.carousel.content, self.carousel.left_arrow, self.carousel.right_arrow):
            self._track_mouse(widget)
        self.grid.carousel = self.carousel
        self.picked = None          # the carousel tile picked to go into the grid
        self._flash_until = 0
        self._last_hands = []

        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(CONTROLS_IDLE_TIME)
        self.idle_timer.timeout.connect(self._SH_IdleTimerTimeout)

        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._update_status)
        self.clock.timeout.connect(self._check_video)
        self._sight_timer = QTimer(self)
        self._sight_timer.setSingleShot(True)
        self._sight_timer.setInterval(2000)
        self._sight_timer.timeout.connect(lambda: self.session.set_video_paused(self.isMinimized() or not self.isVisible()))

        session.stateChanged.connect(self._SH_StateChanged)
        session.participantJoined.connect(self._SH_ParticipantJoined)
        session.participantLeft.connect(self._SH_ParticipantLeft)
        session.participantsChanged.connect(self._update_status)
        session.durationKnown.connect(self._SH_DurationKnown)
        session.audioMutedChanged.connect(self._SH_AudioMutedChanged)
        session.speakersChanged.connect(self._SH_SpeakersChanged)
        session.raisedHandsChanged.connect(self._SH_RaisedHandsChanged)
        session.speakingChanged.connect(self._SH_LocalSpeakingChanged)
        session.mutedByOther.connect(lambda who: self.flash(f'Muted by {who}'))
        session.inviteStatus.connect(self._SH_InviteStatus)
        self._camera_observer = _CameraObserver(self)
        self.setAcceptDrops(True)       # contacts dropped from the contact list are invited
        for participant in session.participants.values():
            self._SH_ParticipantJoined(participant)
        # joined without audio or without video: the controls start that way
        self._SH_AudioMutedChanged(session.audio_muted)
        if session.video_muted:
            self.local_tile.set_camera_off(True)
            self.camera_button.configure('camera', 'Camera off', tint=RED)

        self._center_on_screen()

    def __repr__(self):
        return f'<VideoroomWindow {self.session.room}>'

    def _center_on_screen(self):
        try:
            from blink import Blink
            main_window = Blink().main_window
            screen = main_window.screen() if main_window is not None else None
        except Exception:
            screen = None
        screen = screen or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.move(available.center().x() - self.width() // 2, available.center().y() - self.height() // 2)

    def raise_and_activate(self):
        self.show()
        self.raise_()
        self.activateWindow()

    # Layout

    def resizeEvent(self, event):
        self.grid.setGeometry(self.rect())
        self.call_bar.tile()
        self._place_status()
        self.carousel.tile()
        self.status_label.raise_()
        self.call_bar.raise_()
        super(VideoroomWindow, self).resizeEvent(event)

    def showEvent(self, event):
        super(VideoroomWindow, self).showEvent(event)
        self.show_controls()
        self._update_sight()

    # The controls fade out when the mouse rests

    def show_controls(self):
        if not self.call_bar.shown:
            self.call_bar.fade_in()
        if not self.carousel.shown or self.carousel.isHidden() and self.carousel.tiles:
            self.carousel.fade_in()
        self.idle_timer.start()

    def _SH_IdleTimerTimeout(self):
        if self.session.state != 'terminated' and not self.call_bar.underMouse() and not self.carousel.underMouse():
            self.call_bar.fade_out()
            self.carousel.fade_out()        # hidden with the call bar
            self.set_picked(None)

    def _track_mouse(self, widget):
        widget.setMouseTracking(True)
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.MouseButtonPress):
            self.show_controls()
        if getattr(self, 'carousel', None) is None:
            return False        # still being built: the grid's events come before the rest exists
        if watched is self.local_tile and self.grid.floating and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease):
            if self._drag_floating(event):
                return True
        if event.type() == QEvent.Type.ContextMenu and isinstance(watched, (ParticipantTile, LocalTile)):
            self._tile_menu(watched, event.globalPos())
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if watched in self.carousel.tiles:
                if self.grid.speakers:
                    return True             # the speakers are the room's: changed with Speakers, not here
                if watched.in_grid:         # audio only participants cannot go into the grid
                    self.set_picked(None if watched is self.picked else watched)
                return True
            if self.picked is not None and isinstance(watched, ParticipantTile) and watched in self.grid.tiles:
                picked = self.picked
                self.set_picked(None)
                picked.pinned = True        # the user wants it there: video loss does not take it out
                watched.pinned = False
                self.grid.swap(picked, watched)
                return True
            carousel = self.carousel
            if self.picked is not None and watched not in (carousel, carousel.viewport, carousel.content, carousel.left_arrow, carousel.right_arrow):
                self.set_picked(None)       # a click anywhere else, our own tile included, cancels
        return False

    # Extra videos of ours: a second camera, an RTSP camera (blink.webrtc.extravideo)

    def _add_video_dialog(self):
        number = len(self.session.extra_videos) + 1
        dialog = AddVideoDialog(f'Camera {number}', parent=self)
        if dialog.exec() != AddVideoDialog.DialogCode.Accepted:
            return
        self._start_extra_video(dialog.source(), dialog.label())

    def _multiple_video_feeds(self):
        """Whether the room's gateway takes more videos of ours (it announced multiple-video-feeds)."""
        return 'multiple-video-feeds' in getattr(self.session.connection, 'features', ())

    def _start_pending_extra_videos(self, waited=0):
        """What Join Conference chose besides the camera, once the room is joined: the screen to share, and the other videos."""
        if self._closing or self.session.state != 'established':
            return
        screen = getattr(self.session, 'pending_screen', None)
        if screen:
            self.session.pending_screen = None
            self._screen_pending = True
            self._share_screen(screen)
        sources = getattr(self.session, 'pending_extra_sources', None)
        if not sources:
            return
        if not self.session.video_codec:
            QTimer.singleShot(500, lambda: self._start_pending_extra_videos(waited + 500))     # the answer's codec is not known yet
            return
        if not self._multiple_video_feeds():
            self.session.pending_extra_sources = []
            ActivityLog().warning(f'[videoroom] {self.session.room}: the server takes one video per participant, not publishing {", ".join(label for source, label in sources)}')
            self.flash('This server takes one video per participant', duration=5)
            return
        # the cameras of this computer wait for the screen to take the place of the SDK's camera: it is free then
        from blink.webrtc.videoroom import VideoroomSession
        if getattr(self, '_screen_pending', False) and waited < 30000 and not VideoroomSession.sending_screen_device() and \
                any(source['kind'] == 'camera' for source, label in sources):
            QTimer.singleShot(500, lambda: self._start_pending_extra_videos(waited + 500))
            return
        self._screen_pending = False
        self.session.pending_extra_sources = []
        for source, label in sources:
            self._start_extra_video(source, label)

    def _start_extra_video(self, source, label):
        from blink.webrtc.extravideo import ExtraVideo
        extra = ExtraVideo(self.session, source, label)
        extra.stateChanged.connect(lambda state, reason, extra=extra: self._SH_ExtraVideoStateChanged(extra, state, reason))
        try:
            extra.start()
        except Exception as e:
            self.flash(f'{label}: {e}', duration=5)
            ActivityLog().warning(f'[videoroom] {self.session.room}: cannot add {label}: {e}')
            return
        self._SH_ParticipantJoined(extra.participant)
        self.flash(f'Adding {label}...')

    def _SH_ExtraVideoStateChanged(self, extra, state, reason):
        if self._closing:
            return
        if state == 'established':
            self.flash(f'{extra.label} added')
            self._update_auto_speakers()
        elif state == 'ended':
            self._SH_ParticipantLeft(extra.participant)
            self._update_auto_speakers()
            if reason not in ('stopped', 'left'):
                self.flash(f'{extra.label}: {reason}', duration=6)

    def _show_info(self):
        from blink.webrtc.infopanel import ConferenceInfoPanel
        ConferenceInfoPanel.show_for(self)

    def _set_self_hidden(self, hidden):
        self.grid.set_self_hidden(hidden)
        self.flash('Your video is hidden here; the others still see you' if hidden else 'Your video is back')

    def _drag_floating(self, event):
        """Our floating thumbnail follows the mouse while dragged, and goes to the nearest corner when let go."""
        tile = self.local_tile
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.position().toPoint()
            return True
        if self._drag is None:
            return False
        if event.type() == QEvent.Type.MouseMove:
            position = tile.mapToParent(event.position().toPoint()) - self._drag
            grid = self.grid
            position.setX(max(0, min(position.x(), grid.width() - tile.width())))
            position.setY(max(0, min(position.y(), grid.height() - tile.height())))
            tile.move(position)
            return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            self._drag = None
            self.grid.snap_floating(tile.geometry().center())
            return True
        return False

    def set_picked(self, tile):
        """Pick a carousel tile; the remote tiles in the grid then show they can be replaced by it."""
        if tile is not None and tile not in self.carousel.tiles:
            tile = None
        self.picked = tile
        for candidate in self.grid.tiles:
            if isinstance(candidate, ParticipantTile):
                in_grid = candidate not in self.carousel.tiles
                candidate.set_pick_state(picked=candidate is tile, replaceable=tile is not None and in_grid)

    def mouseMoveEvent(self, event):
        self.show_controls()
        super(VideoroomWindow, self).mouseMoveEvent(event)

    def enterEvent(self, event):
        self.show_controls()
        super(VideoroomWindow, self).enterEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.picked is None:
            self.toggle_full_screen()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.picked is not None:
            self.set_picked(None)
        elif event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_full_screen()
        else:
            super(VideoroomWindow, self).keyPressEvent(event)

    def toggle_full_screen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self.fullscreen_button.configure('fullscreen.exit' if self.isFullScreen() else 'fullscreen',
                                         'Window' if self.isFullScreen() else 'Full screen')

    # Controls

    def _SH_MuteButtonClicked(self):
        self.session.set_audio_muted(not self.session.audio_muted)

    def _SH_AudioMutedChanged(self, muted):
        self.mute_button.configure('mic.slash' if muted else 'mic', 'Unmute' if muted else 'Mute', tint=RED if muted else None)
        self.local_tile.set_muted(muted)

    def _SH_CameraButtonClicked(self):
        off = not self.session.video_muted
        self.session.set_video_muted(off)
        self.local_tile.set_camera_off(off)
        self.camera_button.configure('camera', 'Camera off' if off else 'Camera', tint=RED if off else None)

    def _SH_CameraMenuClicked(self):
        """The video input: each camera, the screen (one entry per monitor the SDK has a device
        for), and turning the camera off or on, as the Preferences camera list has them."""
        from sipsimple.application import SIPApplication
        from sipsimple.configuration.settings import SIPSimpleSettings
        from blink.camera_formats import usable_cameras
        from blink.screen_pointer import is_screen_device, screen_index
        from blink.screen_share import ScreenShareManager
        try:
            devices = list(SIPApplication.engine.video_devices)
        except Exception:
            devices = []
        current = SIPSimpleSettings().video.device
        manager = ScreenShareManager()
        sharing = manager.sharing
        off = self.session.video_muted
        menu = QMenu(self)
        cameras = [('system_default', 'System Default')] + [(device, device) for device in usable_cameras(devices)]
        for device, label in cameras:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not off and not sharing and current == device)
            action.triggered.connect(lambda checked, device=device: self._use_camera(device))
        screens = sorted((device for device in devices if is_screen_device(device)), key=screen_index)
        if screens and (self.session.state == 'established' or sharing):     # sharing needs the room connected
            menu.addSeparator()
            for device in screens:
                action = menu.addAction('Share screen' if len(screens) == 1 else f'Share {device}')
                action.setCheckable(True)
                action.setChecked(not off and sharing and (len(screens) == 1 or current == device))
                action.triggered.connect(lambda checked, device=device: self._share_screen(device))
        menu.addSeparator()
        from blink.webrtc.background_blur import blur_available
        blur = menu.addAction('Blur background')
        blur.setCheckable(True)
        blur.setChecked(blur_available() and SIPSimpleSettings().video.background_blur)
        blur.setEnabled(blur_available() and not sharing)
        blur.setToolTip('Only you are seen, the room behind you is blurred' if blur_available() else 'Needs python3-opencv')
        blur.triggered.connect(self._set_background_blur)
        menu.setToolTipsVisible(True)
        toggle = menu.addAction('Turn camera on' if off else 'Turn camera off')
        toggle.triggered.connect(self._SH_CameraButtonClicked)
        menu.exec(self.camera_button.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    def _set_background_blur(self, enabled):
        from blink.webrtc.background_blur import set_blur_enabled
        set_blur_enabled(enabled)
        ActivityLog().info(f'[videoroom] Background blur {"on" if enabled else "off"}')
        for session, window in list(VideoroomWindow.windows.items()):     # every room sends the same camera
            session.blur_changed()
            window.local_tile.attach_camera()

    def _use_camera(self, device):
        from blink.screen_share import ScreenShareManager, _select_device
        manager = ScreenShareManager()
        if manager.sharing:
            manager.stop_sharing(camera=device)
        else:
            _select_device(device)
        if self.session.video_muted:
            self._SH_CameraButtonClicked()
    def _SH_ShareButtonClicked(self):
        from blink.webrtc.sharedialog import ShareConferenceDialog
        ShareConferenceDialog(self.session.sip_room, self.session.account, target=self.session.target, title=self.title, parent=self).exec()

    def _SH_ChatButtonClicked(self):
        """The room's MSRP chat in front, started if there is none (it ended, or was never there)."""
        from blink.webrtc import room_chat_session
        chat_window = QApplication.instance().chat_window
        session = room_chat_session(self.session)
        if session is not None:
            chat_window.show_session(session)
            return
        from blink.contacts import URIUtils
        from blink.sessions import SessionManager, StreamDescription
        ActivityLog().info(f'[videoroom] {self.session.room}: chat over MSRP to {self.session.sip_room}')
        contact, contact_uri = URIUtils.find_contact(self.session.sip_room, display_name='Conference')
        session = SessionManager().create_session(contact, contact_uri, [StreamDescription('chat')], account=self.session.account)
        if session is not None:
            session.opened_for_videoroom = True     # leaving the video room ends it too
            QTimer.singleShot(600, lambda: chat_window.show_session(session))      # once its tile is in the window

    # Raised hands, muting, removing (as Sylk Mobile)

    def _SH_LocalSpeakingChanged(self, speaking):
        # alone in the room nobody hears us: no green border
        self.local_tile.speaking = speaking and bool(self.tiles)
        self.local_tile.overlay.update()

    def _SH_RaisedHandsChanged(self, ids):
        raised = set(ids)
        self.local_tile.hand = self.session.id in raised
        self.local_tile.overlay.update()
        self.local_tile.place_thumbnail_button()
        for participant, tile in self.tiles.items():
            hand = participant.id in raised
            if hand != tile.hand:
                tile.hand = hand
                tile.update()
        own = self.local_tile.hand
        self._update_speakers_button()
        self.hand_button.configure('hand', 'Lower hand' if own else 'Raise hand', tint=HAND_COLOR if own else None)
        newly = [id for id in ids if id not in getattr(self, '_last_hands', [])]
        self._last_hands = list(ids)
        others = [self.session.speaker_name(id) for id in newly if id != self.session.id]
        if others:
            self.flash(f'{", ".join(others)} raised a hand')

    def _SH_MoreButtonClicked(self):
        menu = QMenu(self)
        from blink.screen_share import ScreenShareManager
        manager = ScreenShareManager()
        if manager.sharing:
            menu.addAction('Stop sharing screen').triggered.connect(self._stop_screen)
        elif self.session.state == 'established':      # sharing needs the room connected
            share_screen = menu.addAction('Share screen')
            share_screen.setToolTip('Send the screen instead of the camera')
            share_screen.setEnabled(manager.can_share_screen())
            share_screen.triggered.connect(lambda: self._share_screen())
        menu.addSeparator()
        if self.grid.self_hidden:
            menu.addAction('Show myself').triggered.connect(lambda: self._set_self_hidden(False))
        elif self.session.state == 'established' and self.grid.can_hide_self:
            hide = menu.addAction('Hide myself')
            hide.setToolTip('Your own video goes from this window; the others still see you')
            hide.triggered.connect(lambda: self._set_self_hidden(True))
        if not self.grid.self_hidden and self.local_tile.thumbnail_button.isEnabled():
            menu.addAction('Put me back in the grid' if self.grid.floating else 'Show me as a thumbnail').triggered.connect(self.grid.toggle_local_placement)
        menu.addSeparator()
        if self.session.state == 'established' and self.session.video_codec and self._multiple_video_feeds():
            menu.addAction('Add video...').triggered.connect(self._add_video_dialog)
        menu.addAction('Invite participants...').triggered.connect(self._invite_dialog)
        menu.addAction('Conference info...').triggered.connect(self._show_info)
        share = menu.addAction('Share conference address...')
        share.setToolTip('The addresses others can join this conference at')
        share.triggered.connect(self._SH_ShareButtonClicked)
        menu.addSeparator()
        mute_all = menu.addAction('Mute everyone')
        mute_all.setToolTip('Everyone else\'s microphone is muted')
        mute_all.triggered.connect(self.session.mute_everyone)
        mute_all.setEnabled(bool(self.session.participants))
        hands = [id for id in self.session.raised_hands if id != self.session.id]
        if hands:
            menu.addAction('Lower all hands').triggered.connect(lambda: [self.session.toggle_hand(id) for id in hands])
        menu.exec(self.more_button.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    # Sharing the screen: it is a video device (blink.screen_share), sent instead of the camera

    def _share_screen(self, device=None):
        from blink.screen_share import ScreenShareManager
        if self.session.state != 'established':
            return
        if self.session.video_muted:
            self._SH_CameraButtonClicked()      # the screen goes where the camera would
        if not ScreenShareManager().share_screen(device):
            self.flash('The screen cannot be shared')
        else:
            self.flash('Choose the screen to share')

    def _stop_screen(self):
        from blink.screen_share import ScreenShareManager
        ScreenShareManager().stop_sharing()

    def _camera_device_changed(self):
        if self._closing:
            return
        self.session.camera_changed()
        self.local_tile.attach_camera()
        from blink.webrtc.videoroom import VideoroomSession
        screen = VideoroomSession.sending_screen_device()
        if self.stop_screen_button.isVisibleTo(self.call_bar) != screen:
            self.stop_screen_button.setVisible(screen)
            self.grid.hide_local = screen
            self.grid.layout_tiles()
            self.carousel.tile()
            self.flash('Sharing your screen' if screen else 'Screen sharing stopped')
            # with nobody chosen as speaker, the screen is what everyone should look at: we are the
            # speaker while it is shared, and the grid comes back after (unless someone changed it)
            if screen and not self.session.speakers:
                self._speaker_for_screen = True
                self._auto_speakers = None
                self._auto_issued = []
                self._update_auto_speakers()
            elif not screen and getattr(self, '_speaker_for_screen', False):
                self._speaker_for_screen = False
                if self.session.speakers and self.session.speakers in getattr(self, '_auto_issued', []):
                    self.session.set_speakers([])
                self._auto_speakers = None
            if screen:
                # out of the way of what is shared; back as it was when the sharing ends
                self._minimized_for_screen = not self.isMinimized()
                self._full_screen_before = self.isFullScreen()
                self.showMinimized()
            elif getattr(self, '_minimized_for_screen', False):
                self._minimized_for_screen = False
                if self.isMinimized():
                    if self._full_screen_before:
                        self.showFullScreen()
                    else:
                        self.showNormal()
                    self.raise_()
                    self.activateWindow()

    def _update_auto_speakers(self):
        """While our screen is shared with nobody chosen as speaker: the screen is the speaker, and with one
        more video of ours (a camera) both are; with more than two videos of ours, nobody is made speaker.
        Followed as our extra videos come and go, until someone chooses the speakers."""
        if not getattr(self, '_speaker_for_screen', False) or self.session.state != 'established':
            return
        current = list(self.session.speakers)
        issued = getattr(self, '_auto_issued', [])     # what we set (the server's answer to one may come after the next)
        if current and current not in issued:
            self._speaker_for_screen = False        # chosen by someone: left alone from now on
            return
        extras = [extra.id for extra in self.session.extra_videos.values() if extra.state == 'established']
        feeds = [self.session.id] + extras
        wanted = feeds if len(feeds) <= 2 else []
        self._auto_speakers = wanted
        self._auto_issued = issued + [wanted]
        if wanted != current:
            self.session.set_speakers(wanted)

    # Inviting (the server calls them: videoroom-invite, then invite-status as it goes)

    def _invite_dialog(self):
        from blink.messagepane.forward import ForwardDialog
        dialog = ForwardDialog(0, parent=self, title='Invite to Conference', prompt='Invite to the conference:',
                               action='Invite', action_many='Invite %d')
        if dialog.exec() == ForwardDialog.DialogCode.Accepted and dialog.keys:
            self._invite(list(dialog.keys), dialog.names)

    def _invite(self, uris, names=None):
        names = names or {}
        uris = [uri for uri in uris if uri]
        if not uris:
            return
        self._invite_names = getattr(self, '_invite_names', {})
        for uri in uris:
            self._invite_names[uri.replace('sip:', '', 1)] = names.get(uri) or uri
        self.session.invite(uris)
        self.flash('Inviting ' + ', '.join(names.get(uri) or uri for uri in uris))

    def _SH_InviteStatus(self, uri, state, code, reason):
        name = getattr(self, '_invite_names', {}).get(uri, uri)
        if state == 'progress':
            self.flash(f'Calling {name}...')
        elif state == 'success':
            self.flash(f'{name} answered')
        elif state == 'failed':
            self.flash(f'{name}: {reason or "not reached"}' + (f' ({code})' if code else ''), duration=5)

    def _drop_invitees(self, mime_data):
        from blink.sessions import ConferenceInviteList
        return ConferenceInviteList.decode(mime_data)

    def dragEnterEvent(self, event):
        from blink.sessions import ConferenceInviteList
        if any(event.mimeData().hasFormat(kind) for kind in ConferenceInviteList.mime_types):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        invitees = self._drop_invitees(event.mimeData())
        if invitees:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._invite([key for key, name in invitees], {key: name for key, name in invitees if name})
        else:
            event.ignore()

    def _tile_menu(self, tile, position):
        session = self.session
        menu = QMenu(self)
        speakers = list(session.speakers)
        if isinstance(tile, LocalTile):
            id = session.id
            if tile.thumbnail_button.isEnabled():
                menu.addAction('Put me back in the grid' if self.grid.floating else 'Show me as a thumbnail').triggered.connect(self.grid.toggle_local_placement)
            if session.state == 'established' and self.grid.can_hide_self:
                hide = menu.addAction('Hide myself')
                hide.setToolTip('Your own video goes from this window; the others still see you')
                hide.triggered.connect(lambda: self._set_self_hidden(True))
            menu.addSeparator()
            menu.addAction('Lower hand' if tile.hand else 'Raise hand').triggered.connect(lambda: session.toggle_hand())
        elif getattr(tile.participant, 'extra_video', None) is not None:
            participant = tile.participant
            id = participant.id
            menu.addAction(participant.name).setEnabled(False)
            menu.addSeparator()
            from blink.webrtc.extravideo import RESOLUTIONS
            sizes = menu.addMenu('Send at')
            for name, width, height, bitrate in RESOLUTIONS:
                action = sizes.addAction(f'{width}x{height}, about {bitrate} kbit/s')
                action.setCheckable(True)
                action.setChecked(participant.extra_video.resolution == name)
                action.triggered.connect(lambda checked, name=name: participant.extra_video.set_resolution(name))
            menu.addAction('Stop this video').triggered.connect(lambda: participant.extra_video.stop())
        else:
            participant = tile.participant
            id = participant.id
            menu.addAction(participant.name).setEnabled(False)
            menu.addSeparator()
            if not participant.parent:      # an extra video has no microphone nor hand of its own
                menu.addAction('Unmute' if participant.muted else 'Mute').triggered.connect(
                    lambda: session.mute_participant(participant, not participant.muted))
            if tile.hand:
                menu.addAction('Lower hand').triggered.connect(lambda: session.toggle_hand(id))
        can_speak = isinstance(tile, LocalTile) or tile.participant.video is True
        if can_speak:
            menu.addSeparator()
            if id in speakers:
                menu.addAction('Stop showing as speaker').triggered.connect(lambda: session.set_speakers([s for s in speakers if s != id]))
            else:
                menu.addAction('Show as the speaker').triggered.connect(lambda: session.set_speakers([id]))
                if len(speakers) == 1:
                    menu.addAction('Show as second speaker').triggered.connect(lambda: session.set_speakers(speakers + [id]))
        if not isinstance(tile, LocalTile) and tile.participant.type == 'sip':
            menu.addSeparator()
            menu.addAction('Remove from the conference').triggered.connect(lambda: session.remove_participants([tile.participant.uri]))
        menu.exec(position)

    def flash(self, text, duration=3):
        """A note in the status pill for a few seconds (the window's title has the rest)."""
        self._flash_until = monotonic() + duration
        self.status_label.set_status(text)
        if self.carousel.isVisible():
            self.status_label.move(self.status_label.x(), self.carousel.y() - 8 - self.status_label.height())     # above the carousel
        QTimer.singleShot(int(duration * 1000) + 50, self._update_status)

    # Speaker selection (blink.webrtc.speakerdialog)

    def _speaker_candidates(self):
        candidates = [(self.session.id, 'Me')]
        candidates += [(participant.id, participant.name) for participant, tile in self.tiles.items()
                       if participant.video is True and participant.visible]
        return candidates

    def _update_speakers_button(self):
        # as Sylk Mobile: when there are two or more others with video to choose between, or speakers to clear
        others = sum(1 for participant in self.tiles if participant.video is True and participant.visible)
        visible = others >= 2 or bool(self.session.speakers)
        changed = False
        if self.speakers_button.isVisibleTo(self.call_bar) != visible:
            self.speakers_button.setVisible(visible)
            changed = True
        # raising a hand is for more than two people (or to lower one still up)
        hand = len(self.tiles) >= 2 or self.session.id in self.session.raised_hands
        if self.hand_button.isVisibleTo(self.call_bar) != hand:
            self.hand_button.setVisible(hand)
            changed = True
        if changed:
            self.carousel.tile()

    def _SH_SpeakersButtonClicked(self):
        from blink.webrtc.speakerdialog import SpeakerSelectionDialog
        SpeakerSelectionDialog(self._speaker_candidates(), self.session.speakers, self.session.set_speakers, parent=self).exec()

    def _SH_SpeakersChanged(self, ids, originator):
        self._apply_speakers()
        self._update_speakers_button()

    def _apply_speakers(self):
        tiles = []
        for id in self.session.speakers:
            if id == self.session.id:
                tiles.append(self.local_tile)
            else:
                # someone's video, or one of our extra videos (not among the room's participants: we publish it)
                extra = self.session.extra_videos.get(id)
                participant = extra.participant if extra is not None else self.session.participants.get(id)
                tile = self.tiles.get(participant) if participant is not None else None
                if tile is not None:
                    tiles.append(tile)
        if tiles != self.grid.speakers:
            self.set_picked(None)
            self.grid.speakers = tiles
            self.grid.layout_tiles()

    def _SH_HangupButtonClicked(self):
        self.close()

    def _end_room_chat(self):
        """Leaving: the MSRP chat to the room that was opened for this video room ends with it, or the
        room would keep us as a participant; a chat that was there before (we escalated) goes on."""
        # found by the room's MSRP address: once the video room has ended room_chat_session() no longer finds it
        from blink.sessions import SessionManager
        room = self.session.sip_room.replace('sip:', '', 1).lower()
        for session in list(SessionManager().sessions):
            contact_uri = getattr(session, 'contact_uri', None)
            if contact_uri is None or not getattr(session, 'opened_for_videoroom', False):
                continue
            address = str(contact_uri.uri).replace('sip:', '', 1).split(';', 1)[0].lower()
            state = str(session.state or '')
            if address != room or state in ('ending', 'ended', 'deleted') or state.startswith('end'):
                continue
            session.opened_for_videoroom = False
            ActivityLog().info(f'[videoroom] {self.session.room}: the video room is over, the chat opened with it ends too')
            session.end()

    def closeEvent(self, event):
        if self.session.state not in ('idle', 'terminated'):
            self._end_room_chat()       # while the room still finds its chat (room_chat_session)
            self.session.leave()
        self._release()
        super(VideoroomWindow, self).closeEvent(event)

    def _release(self):
        if self._closing:
            return
        self._closing = True
        self.clock.stop()
        self._camera_observer.close()
        try:
            from blink.screen_share import ScreenShareManager
            QTimer.singleShot(0, ScreenShareManager().videoroom_ended)     # once the session is terminated
        except Exception:
            pass
        self.local_tile.release()
        for participant, tile in self.tiles.items():
            try:
                participant.changed.disconnect(self._SH_ParticipantChanged)
            except TypeError:
                pass
            tile.release()
        self.windows.pop(self.session, None)

    # Session

    def _SH_StateChanged(self, state, reason):
        if state == 'accepted':
            self.status_label.set_status('Joining…')
        elif state == 'established':
            if self.joined_at is None:
                from time import monotonic
                self.joined_at = monotonic()
            self.clock.start()
            self._update_status()
            self.idle_timer.start()
            self._start_pending_extra_videos()
        elif state == 'terminated':
            self.clock.stop()
            self._end_room_chat()       # however it ended, the chat opened for it goes with it
            if self._closing:
                return
            if reason == 'left':
                self.close()
                return
            self.status_label.set_status(reason[:1].upper() + reason[1:] if reason else 'Ended')
            self.call_bar.fade_in()
            QTimer.singleShot(ENDED_LINGER, self.close)

    def _place_status(self):
        if self.status_label.isVisibleTo(self):
            self.status_label.set_status(self.status_label.text())     # it sits above the call bar

    def _SH_DurationKnown(self, seconds):
        self.duration = seconds

    def _SH_ParticipantJoined(self, participant):
        if not participant.visible or participant in self.tiles:
            return
        tile = ParticipantTile(participant, self.grid)
        tile.placementChanged = self._SH_ParticipantChanged
        tile.refreshVideo = lambda: self.session.refresh_video(participant)
        self.tiles[participant] = tile
        participant.changed.connect(self._SH_ParticipantChanged)
        self._track_mouse(tile)
        self.grid.add(tile)
        if self.session.speakers:
            self._apply_speakers()      # a speaker the room named before we knew them
        if participant.id in self.session.raised_hands:
            tile.hand = True
        self._update_speakers_button()
        self._update_status()

    def _SH_ParticipantLeft(self, participant):
        tile = self.tiles.pop(participant, None)
        if tile is not None:
            try:
                participant.changed.disconnect(self._SH_ParticipantChanged)
            except TypeError:
                pass
            if self.picked is not None:
                self.set_picked(None)
            tile.release()
            self.grid.remove(tile)
            self.grid.speakers = [speaker for speaker in self.grid.speakers if speaker is not tile]
            if not self.tiles and self.local_tile.speaking:
                self._SH_LocalSpeakingChanged(False)     # alone now
        self._update_speakers_button()
        self._update_status()

    def _check_video(self):
        now = monotonic()
        for tile in list(self.tiles.values()):
            if self.session.video_paused:
                tile._sample = None         # no video on purpose: not a loss, and a new warm-up after
                continue
            tile.check_video(now)

    # The video of the feeds pauses while nobody can see it (as Sylk Mobile and the Cocoa window)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_sight()
        super(VideoroomWindow, self).changeEvent(event)

    def hideEvent(self, event):
        self._update_sight()
        super(VideoroomWindow, self).hideEvent(event)

    def _update_sight(self):
        hidden = self.isMinimized() or not self.isVisible()
        if hidden:
            self._sight_timer.start()       # a moment out of sight (switching desktops) is not worth it
        else:
            self._sight_timer.stop()
            self.session.set_video_paused(False)

    def _SH_ParticipantChanged(self):
        # the feed's offer says whether there is video: an audio only participant goes to the carousel
        if self.picked is not None and not self.picked.in_grid:
            self.set_picked(None)
        self.grid.layout_tiles()
        self._update_speakers_button()

    def _update_status(self):
        if self.session.state != 'established':
            return
        people = sum(1 for participant in self.session.participants.values() if participant.visible and not participant.parent) + 1     # an extra video is not a participant
        parts = [self.title, '%d %s' % (people, 'participant' if people == 1 else 'participants')]
        if self.joined_at is not None:
            from time import monotonic
            elapsed = int(monotonic() - self.joined_at) + (self.duration or 0)
            hours, rest = divmod(elapsed, 3600)
            minutes, seconds = divmod(rest, 60)
            parts.append('%d:%02d:%02d' % (hours, minutes, seconds) if hours else '%02d:%02d' % (minutes, seconds))
        if monotonic() >= getattr(self, '_flash_until', 0):
            self.status_label.set_status(None)     # once connected the title says it; the pill is for connecting, ending and notes
        self.setWindowTitle('  ·  '.join(parts))


class AddVideoDialog(QDialog):
    """Another video of ours for the room: a camera of this computer, or an RTSP camera (CCTV)."""

    def __init__(self, label, parent=None):
        super(AddVideoDialog, self).__init__(parent)
        from blink.webrtc.extravideo import local_cameras, saved_sources
        self.setWindowTitle('Add video')
        self.setMinimumWidth(440)
        layout = QFormLayout(self)
        self.label_edit = QLineEdit(label, self)
        self.label_edit.setPlaceholderText('Camera 1')
        layout.addRow('Name', self.label_edit)

        self.camera_radio = QRadioButton('Camera of this computer', self)
        self.rtsp_radio = QRadioButton('Network camera (RTSP)', self)
        layout.addRow(self.camera_radio)
        self.camera_combo = QComboBox(self)
        try:
            self.cameras = local_cameras()
        except Exception:
            self.cameras = []
        for name, device in self.cameras:
            self.camera_combo.addItem(name)
        layout.addRow('Camera', self.camera_combo)
        layout.addRow(self.rtsp_radio)
        self.saved = saved_sources()
        self.saved_combo = QComboBox(self)
        self.saved_combo.addItem('New...')
        for source in self.saved:
            self._add_saved(source)
        # the addresses are long: the combo is not as wide as they are, each shows as name and host, the address in its tooltip
        self.saved_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.saved_combo.setMinimumContentsLength(24)
        self.saved_combo.currentIndexChanged.connect(self._SH_SavedChanged)
        saved_row = QHBoxLayout()
        saved_row.addWidget(self.saved_combo, 1)
        find_button = QPushButton('Find on network...', self)
        find_button.setAutoDefault(False)
        find_button.setToolTip('Cameras on your network (ONVIF, Bonjour), tested before they are added')
        find_button.clicked.connect(self._find_camera)
        saved_row.addWidget(find_button)
        layout.addRow('Used before', saved_row)
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText('rtsp://192.168.1.10:554/stream1')
        layout.addRow('Address', self.url_edit)
        self.user_edit = QLineEdit(self)
        layout.addRow('User', self.user_edit)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow('Password', self.password_edit)
        self.resolution_combo = QComboBox(self)
        from blink.webrtc.extravideo import DEFAULT_RESOLUTION, RESOLUTIONS
        for name, width, height, bitrate in RESOLUTIONS:
            self.resolution_combo.addItem(f'{width}x{height}, about {bitrate} kbit/s', name)
        self.resolution_combo.setCurrentIndex(self.resolution_combo.findData(DEFAULT_RESOLUTION))
        layout.addRow('Send at', self.resolution_combo)
        note = QLabel('Video only, 10 fps. A network camera that works is remembered here, with its password.', self)
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Add')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        for widget in (self.camera_radio, self.rtsp_radio):
            widget.toggled.connect(self._update)
        self.url_edit.textChanged.connect(self._update)
        (self.rtsp_radio if self.saved or not self.cameras else self.camera_radio).setChecked(True)
        if self.saved:
            self.saved_combo.setCurrentIndex(1)
        self.camera_radio.setEnabled(bool(self.cameras))
        self._update()

    def _add_saved(self, source):
        from urllib.parse import urlsplit
        try:
            host = urlsplit(source['url']).hostname or ''
        except ValueError:
            host = ''
        self.saved_combo.addItem(f'{source.get("label") or "Camera"}' + (f' ({host})' if host else ''))
        self.saved_combo.setItemData(self.saved_combo.count() - 1, source['url'], Qt.ItemDataRole.ToolTipRole)

    def _find_camera(self):
        from blink.webrtc.camerasetup import NetworkCameraDialog
        from blink.webrtc.extravideo import saved_sources
        dialog = NetworkCameraDialog(self.label_edit.text().strip(), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.saved = saved_sources()       # it is the first of them now
        self.saved_combo.blockSignals(True)
        while self.saved_combo.count() > 1:
            self.saved_combo.removeItem(1)
        for source in self.saved:
            self._add_saved(source)
        self.saved_combo.blockSignals(False)
        self.rtsp_radio.setChecked(True)
        self.saved_combo.setCurrentIndex(1)
        self._SH_SavedChanged(1)
        self._update()

    def _SH_SavedChanged(self, index):
        if index <= 0:
            return
        source = self.saved[index - 1]
        self.url_edit.setText(source['url'])
        self.user_edit.setText(source.get('user') or '')
        self.password_edit.setText(source.get('password') or '')
        index = self.resolution_combo.findData(source.get('resolution'))
        if index >= 0:
            self.resolution_combo.setCurrentIndex(index)
        if source.get('label'):
            self.label_edit.setText(source['label'])

    def _update(self):
        rtsp = self.rtsp_radio.isChecked()
        self.camera_combo.setEnabled(not rtsp)
        for widget in (self.saved_combo, self.url_edit, self.user_edit, self.password_edit):
            widget.setEnabled(rtsp)
        self.ok_button.setEnabled(self.url_edit.text().strip().lower().startswith(('rtsp://', 'rtsps://')) if rtsp else bool(self.cameras))

    def label(self):
        return self.label_edit.text().strip() or self.label_edit.placeholderText()

    def source(self):
        if self.rtsp_radio.isChecked():
            return dict(kind='rtsp', url=self.url_edit.text().strip(), user=self.user_edit.text().strip(), password=self.password_edit.text(),
                        resolution=self.resolution_combo.currentData())
        name, device = self.cameras[self.camera_combo.currentIndex()]
        return dict(kind='camera', name=name, device=device, resolution=self.resolution_combo.currentData())

