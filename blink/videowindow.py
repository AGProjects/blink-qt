"""The video call window: a window of its own for every call with video.

It used to be drawn inside the MSRP sessions window, over the chat. Now, as in
Blink for macOS, the remote picture fills a window of its own, the local camera
is a thumbnail in one of its corners and the call controls are a translucent
segmented bar floating at the bottom, which fades out when the mouse rests.

The bar, left to right: Mute, Camera (a menu: preview, swap, stop video, aspect
ratio, then the cameras), Chat (opens the call in the MSRP sessions window),
Screenshot, Record, Full screen, Info (the call info panel in the main window)
and End, last and in red. Hold is not on the bar (as on macOS) but is in the
right-click menu.

The thumbnail is placed from three things only: the corner the user chose, its
size as a fraction of the window's width and the camera's shape; it is laid out
again whenever any of those or the window change, so it can never end up
outside the window. Dragging it moves it to the nearest corner, dragging its
inner corner resizes it; both are remembered.
"""

import os

from datetime import datetime
from itertools import count
from math import ceil, floor

from PyQt6.QtCore import Qt, QEvent, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSettings, QSize, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QRegion
from PyQt6.QtWidgets import QAbstractButton, QApplication, QGraphicsOpacityEffect, QLabel, QMenu, QPushButton, QWidget

from application.notification import IObserver, NotificationCenter, ObserverWeakrefProxy
from application.python import Null
from application.system import makedirs
from zope.interface import implementer

from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from blink.configuration.settings import BlinkSettings
from blink.logging import ActivityLog
from blink.resources import Resources
from blink.util import call_in_gui_thread, run_in_gui_thread, translate
from blink.widgets.video import VideoSurface


__all__ = ['VideoWindow', 'VideoWindowManager', 'VideoScreenshot']


# The call bar -- the same measures as the macOS one
CALL_BAR_BOTTOM = 20
CALL_BAR_HEIGHT = 50
CALL_BAR_COMPACT_HEIGHT = 38
CALL_BAR_PADDING = 4
CALL_BAR_RADIUS = 14
CALL_BAR_SEGMENT_MIN_W = 58
CALL_BAR_SEGMENT_COMPACT_W = 40
CALL_BAR_ICON_W = 24
CALL_BAR_ICON_H = 18
CALL_BAR_ICON_GAP = 2
CALL_BAR_LABEL_PT = 8.5
CALL_BAR_MARGIN = 24          # air at the bar's ends before it gives up its labels

RED = QColor(255, 59, 48)     # the system red

# The local camera thumbnail
MY_VIDEO_CORNER_KEY = 'video_window/preview_corner'
MY_VIDEO_SCALE_KEY = 'video_window/preview_scale'
MY_VIDEO_MARGIN = 10
MY_VIDEO_DEFAULT_SCALE = 0.22
MY_VIDEO_MIN_W = 96
MY_VIDEO_MAX_FRACTION = 0.5
MY_VIDEO_RESIZE_GRIP = 18
MY_VIDEO_MIN_ASPECT = 0.5
MY_VIDEO_MAX_ASPECT = 2.5
MY_VIDEO_RADIUS = 10

IDLE_TIME = 3000              # ms without the mouse moving before the bar fades out
TOAST_SECONDS = 6
TOAST_HEIGHT = 30
TOAST_PADDING = 14
TOAST_GAP = 12

DEFAULT_WINDOW_WIDTH = 720

ASPECT_RATIOS = [(4/3, '4:3'), (16/9, '16:9')]


def white(alpha):
    return QColor(255, 255, 255, int(round(255 * alpha)))


# Icons
#
# Drawn here rather than loaded: the bar needs white silhouettes that read on
# any picture, at the bar's own size, and tinted only when the colour means
# something (muted, recording). Each one draws into a 24x18 box.

def _icon_mic(painter, slash=False):
    painter.drawRoundedRect(QRectF(9.5, 1, 5, 9.5), 2.5, 2.5)
    path = QPainterPath()
    path.moveTo(6.5, 7.5)
    path.cubicTo(6.5, 11.5, 9, 13.2, 12, 13.2)
    path.cubicTo(15, 13.2, 17.5, 11.5, 17.5, 7.5)
    painter.strokePath(path, painter.pen())
    painter.drawLine(QPointF(12, 13.2), QPointF(12, 16.5))
    painter.drawLine(QPointF(9, 16.5), QPointF(15, 16.5))
    if slash:
        painter.drawLine(QPointF(5.5, 1.5), QPointF(18.5, 16.5))


def _icon_camera(painter):
    painter.drawRoundedRect(QRectF(2.5, 4.5, 13, 10), 2.5, 2.5)
    lens = QPainterPath()
    lens.moveTo(16.5, 8.2)
    lens.lineTo(21.5, 5.5)
    lens.lineTo(21.5, 13.5)
    lens.lineTo(16.5, 10.8)
    lens.closeSubpath()
    painter.drawPath(lens)


def _icon_chat(painter):
    path = QPainterPath()
    path.addRoundedRect(QRectF(3, 2.5, 18, 11), 4, 4)
    tail = QPainterPath()
    tail.moveTo(7, 12)
    tail.lineTo(6, 16.5)
    tail.lineTo(11.5, 12.5)
    tail.closeSubpath()
    painter.drawPath(path.united(tail))


def _icon_screenshot(painter):
    pen = painter.pen()
    for x, y, dx, dy in ((4, 2.5, 1, 1), (20, 2.5, -1, 1), (4, 15.5, 1, -1), (20, 15.5, -1, -1)):
        path = QPainterPath()
        path.moveTo(x, y + 4 * dy)
        path.lineTo(x, y)
        path.lineTo(x + 4 * dx, y)
        painter.strokePath(path, pen)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(pen.color())
    painter.drawEllipse(QPointF(12, 9), 2.6, 2.6)
    painter.restore()


def _icon_record(painter, active=False):
    painter.drawEllipse(QPointF(12, 9), 7, 7)
    color = painter.pen().color() if not active else RED
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(12, 9), 4, 4)
    painter.restore()


def _icon_fullscreen(painter, exit=False):
    pen = painter.pen()
    # two diagonal arrows, outward (enter) or inward (exit)
    for (ax, ay), (bx, by) in (((5, 15), (10, 10)), ((19, 3), (14, 8))):
        if exit:
            (ax, ay), (bx, by) = (bx, by), (ax, ay)
        painter.drawLine(QPointF(ax, ay), QPointF(bx, by))
        dx = 1 if ax > bx else -1
        dy = 1 if ay > by else -1
        head = QPainterPath()
        head.moveTo(ax - 4.5 * dx, ay)
        head.lineTo(ax, ay)
        head.lineTo(ax, ay - 4.5 * dy)
        painter.strokePath(head, pen)


def _icon_info(painter):
    painter.drawEllipse(QPointF(12, 9), 7.5, 7.5)
    painter.drawLine(QPointF(12, 8), QPointF(12, 12.8))
    color = painter.pen().color()
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(12, 5.3), 1.1, 1.1)
    painter.restore()


def _icon_hangup(painter):
    path = QPainterPath()
    path.moveTo(2.5, 11.5)
    path.cubicTo(2.5, 7.5, 7, 5.5, 12, 5.5)
    path.cubicTo(17, 5.5, 21.5, 7.5, 21.5, 11.5)
    path.lineTo(21.5, 12.5)
    path.lineTo(17.3, 12.5)
    path.lineTo(16.3, 9.6)
    path.cubicTo(13.5, 8.8, 10.5, 8.8, 7.7, 9.6)
    path.lineTo(6.7, 12.5)
    path.lineTo(2.5, 12.5)
    path.closeSubpath()
    color = painter.pen().color()
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)
    painter.restore()


def _icon_pause(painter):
    color = painter.pen().color()
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(7.5, 3, 3.2, 12), 1, 1)
    painter.drawRoundedRect(QRectF(13.3, 3, 3.2, 12), 1, 1)
    painter.restore()


ICONS = {
    'mic': _icon_mic,
    'mic.slash': lambda painter: _icon_mic(painter, slash=True),
    'camera': _icon_camera,
    'chat': _icon_chat,
    'screenshot': _icon_screenshot,
    'record': _icon_record,
    'record.active': lambda painter: _icon_record(painter, active=True),
    'fullscreen': _icon_fullscreen,
    'fullscreen.exit': lambda painter: _icon_fullscreen(painter, exit=True),
    'info': _icon_info,
    'hangup': _icon_hangup,
    'pause': _icon_pause,
}


def draw_icon(painter, name, rect, color):
    """Draw the named icon fitted into rect, in color."""
    function = ICONS.get(name)
    if function is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = min(rect.width() / CALL_BAR_ICON_W, rect.height() / CALL_BAR_ICON_H)
    painter.translate(rect.center().x() - CALL_BAR_ICON_W * scale / 2, rect.center().y() - CALL_BAR_ICON_H * scale / 2)
    painter.scale(scale, scale)
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    function(painter)
    painter.restore()


# The call bar

class CallBarSegment(QAbstractButton):
    """One control on the call bar: an icon over a short label."""

    def __init__(self, icon, label, destructive=False, parent=None):
        super(CallBarSegment, self).__init__(parent)
        self.icon_name = icon
        self.label = label
        self.tint = None
        self.destructive = destructive
        self.hovering = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(label)
        self.setAccessibleName(label)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def configure(self, icon, label, tint=None):
        changed = label != self.label
        self.icon_name = icon
        self.label = label
        self.tint = tint
        self.setAccessibleName(label)
        self.update()
        if changed:
            self._retile()

    @property
    def compact(self):
        bar = self.parent()
        return isinstance(bar, CallBar) and bar.compact

    @staticmethod
    def label_font():
        font = QFont(QApplication.font())
        font.setPointSizeF(CALL_BAR_LABEL_PT)
        font.setWeight(QFont.Weight.Medium)
        return font

    def preferred_width(self, compact):
        if compact:
            return CALL_BAR_SEGMENT_COMPACT_W
        return max(CALL_BAR_SEGMENT_MIN_W, QFontMetrics(self.label_font()).horizontalAdvance(self.label) + 18)

    def _retile(self):
        bar = self.parent()
        if isinstance(bar, CallBar):
            bar.tile()

    def setVisible(self, visible):
        was = self.isVisibleTo(self.parent()) if self.parent() is not None else self.isVisible()
        super(CallBarSegment, self).setVisible(visible)
        if was != bool(visible):
            self._retile()

    def enterEvent(self, event):
        self.hovering = True
        self.update()
        super(CallBarSegment, self).enterEvent(event)

    def leaveEvent(self, event):
        self.hovering = False
        self.update()
        super(CallBarSegment, self).leaveEvent(event)

    def sizeHint(self):
        return QSize(self.preferred_width(self.compact), CALL_BAR_HEIGHT - 2 * CALL_BAR_PADDING)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        enabled = self.isEnabled()
        pressed = enabled and self.isDown()
        radius = CALL_BAR_RADIUS - 5
        if self.destructive:
            color = QColor(RED)
            color.setAlphaF(0.65 if pressed else (0.95 if self.hovering else 0.85))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, radius, radius)
        elif pressed or (self.hovering and enabled):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(white(0.22 if pressed else 0.10))
            painter.drawRoundedRect(rect, radius, radius)

        alpha = 1.0 if enabled else 0.35
        color = QColor(self.tint) if self.tint is not None else QColor(Qt.GlobalColor.white)
        color.setAlphaF(color.alphaF() * alpha)

        width, height = self.width(), self.height()
        if self.compact:
            icon_rect = QRectF((width - CALL_BAR_ICON_W) / 2, (height - CALL_BAR_ICON_H) / 2, CALL_BAR_ICON_W, CALL_BAR_ICON_H)
            draw_icon(painter, self.icon_name, icon_rect, color)
            return
        font = self.label_font()
        metrics = QFontMetrics(font)
        label_h = metrics.height()
        block = CALL_BAR_ICON_H + CALL_BAR_ICON_GAP + label_h
        top = floor((height - block) / 2)
        icon_rect = QRectF(floor((width - CALL_BAR_ICON_W) / 2), top, CALL_BAR_ICON_W, CALL_BAR_ICON_H)
        draw_icon(painter, self.icon_name, icon_rect, color)
        label_color = QColor(self.tint) if self.tint is not None else white(0.92)
        label_color.setAlphaF(min(label_color.alphaF(), 0.92) * alpha)
        painter.setFont(font)
        painter.setPen(label_color)
        label_rect = QRectF(0, top + CALL_BAR_ICON_H + CALL_BAR_ICON_GAP, width, label_h)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self.label)


class CallBar(QWidget):
    """The translucent capsule the segments sit in, centred at the bottom."""

    def __init__(self, parent):
        super(CallBar, self).__init__(parent)
        self.segments = []
        self.compact = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self.opacity_effect)
        self.fade_animation = QPropertyAnimation(self.opacity_effect, b'opacity', self)
        self.fade_animation.finished.connect(self._SH_FadeFinished)
        self.shown = True

    def add_segment(self, icon, label, destructive=False):
        segment = CallBarSegment(icon, label, destructive, self)
        self.segments.append(segment)
        self.tile()
        return segment

    @property
    def visible_segments(self):
        return [segment for segment in self.segments if segment.isVisibleTo(self)]

    def width_for(self, compact):
        return 2 * CALL_BAR_PADDING + sum(segment.preferred_width(compact) for segment in self.visible_segments)

    def minimum_window_width(self):
        """Narrower than this and not even the icons fit: the bar goes."""
        return self.width_for(True) + CALL_BAR_MARGIN

    def tile(self):
        container = self.parent()
        available = container.width() if container is not None else None
        self.compact = available is not None and self.width_for(False) + CALL_BAR_MARGIN > available
        height = CALL_BAR_COMPACT_HEIGHT if self.compact else CALL_BAR_HEIGHT
        x = CALL_BAR_PADDING
        for segment in self.visible_segments:
            width = segment.preferred_width(self.compact)
            segment.setGeometry(x, CALL_BAR_PADDING, width, height - 2 * CALL_BAR_PADDING)
            segment.update()
            x += width
        width = x + CALL_BAR_PADDING
        if container is not None:
            self.setGeometry((container.width() - width) // 2, container.height() - CALL_BAR_BOTTOM - height, width, height)
        else:
            self.resize(width, height)
        self.update()

    def fade_in(self):
        self._fade(1.0, 180)

    def fade_out(self):
        self._fade(0.0, 250)

    def _fade(self, value, duration):
        self.shown = value > 0
        if self.shown:
            # a faded bar does not take clicks: they go to the video, which brings it back
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.fade_animation.stop()
        self.fade_animation.setDuration(duration)
        self.fade_animation.setStartValue(self.opacity_effect.opacity())
        self.fade_animation.setEndValue(value)
        self.fade_animation.start()

    def _SH_FadeFinished(self):
        if not self.shown:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(white(0.16), 1))
        painter.setBrush(QColor(0, 0, 0, int(255 * 0.45)))
        painter.drawRoundedRect(rect, CALL_BAR_RADIUS, CALL_BAR_RADIUS)
        # hairlines between neighbours, but not against the red End
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(white(0.14))
        inset = 8 if self.compact else 10
        visible = self.visible_segments
        for left, right in zip(visible, visible[1:]):
            if left.destructive or right.destructive:
                continue
            painter.drawRect(QRect(right.x(), inset, 1, self.height() - 2 * inset))


class VideoToast(QWidget):
    """A short note over the video that something happened, with at most one thing to do about it."""

    def __init__(self, parent):
        super(VideoToast, self).__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.label = QLabel(self)
        self.label.setStyleSheet('QLabel { color: rgba(255, 255, 255, 235); background: transparent; }')
        self.action_button = QPushButton(self)
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.setFlat(True)
        self.action_button.setStyleSheet('QPushButton { color: #64b5ff; background: transparent; border: none; font-weight: bold; padding: 0px; }'
                                         'QPushButton:hover { color: #9fd0ff; }')
        self.action_button.clicked.connect(self._SH_ActionClicked)
        self.callback = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text, action_title=None, callback=None, seconds=TOAST_SECONDS):
        self.label.setText(text)
        self.callback = callback
        self.action_button.setVisible(bool(action_title))
        self.action_button.setText(action_title or '')
        self.tile()
        self.show()
        self.raise_()
        self.timer.start(int(seconds * 1000))

    def tile(self):
        self.label.adjustSize()
        x = TOAST_PADDING
        self.label.move(x, (TOAST_HEIGHT - self.label.height()) // 2)
        x += self.label.width()
        if self.action_button.isVisibleTo(self):
            self.action_button.adjustSize()
            x += 12
            self.action_button.move(x, (TOAST_HEIGHT - self.action_button.height()) // 2)
            x += self.action_button.width()
        width = x + TOAST_PADDING
        parent = self.parent()
        bar = getattr(parent, 'call_bar', None)
        bottom = bar.y() if bar is not None else parent.height() - CALL_BAR_BOTTOM
        self.setGeometry((parent.width() - width) // 2, bottom - TOAST_GAP - TOAST_HEIGHT, width, TOAST_HEIGHT)

    def _SH_ActionClicked(self):
        callback, self.callback = self.callback, None
        self.hide()
        if callback is not None:
            callback()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(white(0.16), 1))
        painter.setBrush(QColor(0, 0, 0, int(255 * 0.6)))
        radius = TOAST_HEIGHT / 2
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


class StatusLabel(QLabel):
    """The pill in the middle of the picture saying what the call is doing (Connecting..., On hold)."""

    def __init__(self, parent):
        super(StatusLabel, self).__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        self.setFont(font)
        self.setStyleSheet('QLabel { color: white; padding: 8px 18px; }')
        self.hide()

    def set_status(self, text):
        if not text:
            self.hide()
            return
        self.setText(text)
        self.adjustSize()
        parent = self.parent()
        self.move((parent.width() - self.width()) // 2, (parent.height() - self.height()) // 2)
        self.show()
        self.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, int(255 * 0.55)))
        radius = self.height() / 2
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
        painter.end()
        super(StatusLabel, self).paintEvent(event)


# Video surfaces that say when pictures arrive

class TrackedVideoSurface(VideoSurface):
    """A VideoSurface that tells when the first frame after a producer change arrives.

    firstFrame is emitted from the renderer's thread; the signal queues it to the GUI.
    """

    firstFrame = pyqtSignal(int, int)

    def __init__(self, parent=None, framerate=None):
        super(TrackedVideoSurface, self).__init__(parent, framerate=framerate)
        self.frame_count = 0
        self.frame_size = None

    def _set_tracked_producer(self, producer):
        self.frame_count = 0
        self.frame_size = None
        VideoSurface.producer.fset(self, producer)
        if producer is None:
            self._image = None
        self.update()

    producer = property(VideoSurface.producer.fget, _set_tracked_producer)

    def _handle_frame(self, frame):
        super(TrackedVideoSurface, self)._handle_frame(frame)
        self.frame_count += 1
        if self.frame_count == 1:
            self.frame_size = (frame.width, frame.height)
            self.firstFrame.emit(frame.width, frame.height)


def describe_producer(producer):
    """One line about a video producer, for the Activity log."""
    if producer is None:
        return 'none'
    details = [type(producer).__name__]
    for attribute in ('name', 'framesize', 'framerate'):
        try:
            value = getattr(producer, attribute)
        except Exception as e:
            value = '<%s>' % e.__class__.__name__
        details.append('%s=%s' % (attribute, value))
    return ' '.join(details)


# The local camera thumbnail

class LocalVideoView(TrackedVideoSurface):
    """The thumbnail in a corner of the call window.

    It has no position of its own: it is always derived from the corner the
    user chose, its size (a fraction of the window's width) and the camera's
    shape, and laid out again whenever any of them or the window changes.
    """

    def __init__(self, parent, framerate=None):
        super(LocalVideoView, self).__init__(parent, framerate=framerate)
        self.interactive = False      # VideoSurface's own move/resize is not used
        self.mirror = True
        self.drag_mode = None
        self.drag_start_point = None
        self.drag_start_geometry = None
        self.dragged = False
        self.top_inset = MY_VIDEO_MARGIN
        self.bottom_obstacle = None   # a callable returning the call bar's rect, or None
        self.move_animation = QPropertyAnimation(self, b'geometry', self)
        self.move_animation.setDuration(180)
        self.placeholder = ''         # said inside the thumbnail while there is no picture from the camera

    # placement

    @property
    def camera_aspect(self):
        producer = self.producer
        try:
            width, height = producer.framesize
        except (AttributeError, TypeError, ValueError):
            return 16 / 9
        if width < 16 or height < 16:
            return 16 / 9
        aspect = width / height
        return aspect if MY_VIDEO_MIN_ASPECT <= aspect <= MY_VIDEO_MAX_ASPECT else 16 / 9

    @staticmethod
    def corner():
        corner = QSettings().value(MY_VIDEO_CORNER_KEY, 'TR')
        return corner if corner in ('TL', 'TR', 'BL', 'BR') else 'TR'

    @staticmethod
    def scale():
        try:
            value = float(QSettings().value(MY_VIDEO_SCALE_KEY, MY_VIDEO_DEFAULT_SCALE))
        except (TypeError, ValueError):
            value = MY_VIDEO_DEFAULT_SCALE
        return value if value > 0 else MY_VIDEO_DEFAULT_SCALE

    def target_geometry(self, corner=None, scale=None):
        container = self.parent()
        if container is None:
            return self.geometry()
        W, H = container.width(), container.height()
        corner = corner or self.corner()
        scale = scale if scale is not None else self.scale()
        aspect = self.camera_aspect

        w = min(max(W * scale, MY_VIDEO_MIN_W), W * MY_VIDEO_MAX_FRACTION)
        h = w / aspect
        if h > H * MY_VIDEO_MAX_FRACTION:
            h = H * MY_VIDEO_MAX_FRACTION
            w = h * aspect

        x = MY_VIDEO_MARGIN if corner[1] == 'L' else W - w - MY_VIDEO_MARGIN
        if corner[0] == 'T':
            y = self.top_inset
        else:
            y = H - h - MY_VIDEO_MARGIN
            obstacle = self.bottom_obstacle() if self.bottom_obstacle is not None else None
            if obstacle is not None and not (x + w <= obstacle.left() or x >= obstacle.right()):
                y = obstacle.top() - MY_VIDEO_MARGIN - h
        # whatever the insets asked for, the thumbnail stays inside
        x = min(max(x, 0), max(W - w, 0))
        y = min(max(y, 0), max(H - h, 0))
        return QRect(int(floor(x)), int(floor(y)), int(floor(w)), int(floor(h)))

    def layout_in_parent(self, animate=False):
        if self.drag_mode is not None:
            return                    # the mouse owns the geometry until it lets go
        geometry = self.target_geometry()
        if animate:
            self.move_animation.stop()
            self.move_animation.setStartValue(self.geometry())
            self.move_animation.setEndValue(geometry)
            self.move_animation.start()
        else:
            self.move_animation.stop()
            self.setGeometry(geometry)

    # rounded corners

    def resizeEvent(self, event):
        super(LocalVideoView, self).resizeEvent(event)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), MY_VIDEO_RADIUS, MY_VIDEO_RADIUS)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def set_placeholder(self, text):
        self.placeholder = text
        self.update()

    def paintEvent(self, event):
        super(LocalVideoView, self).paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._image is None and self.placeholder:
            font = painter.font()
            font.setPointSizeF(max(font.pointSizeF() * 0.85, 7))
            painter.setFont(font)
            painter.setPen(white(0.75))
            painter.drawText(QRectF(self.rect()).adjusted(6, 4, -6, -4), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.placeholder)
        painter.setPen(QPen(white(0.35), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), MY_VIDEO_RADIUS, MY_VIDEO_RADIUS)

    # moving and resizing

    def grip_rect(self):
        """The corner that points into the picture: drag it to resize."""
        g = MY_VIDEO_RESIZE_GRIP
        corner = self.corner()
        x = self.width() - g if corner[1] == 'L' else 0
        y = self.height() - g if corner[0] == 'T' else 0
        return QRect(x, y, g, g)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.parent() is None:
            event.ignore()
            return
        self.drag_mode = 'resize' if self.grip_rect().contains(event.position().toPoint()) else 'move'
        self.drag_start_point = self.mapToParent(event.position().toPoint())
        self.drag_start_geometry = self.geometry()
        self.dragged = False
        self.move_animation.stop()
        event.accept()

    def mouseMoveEvent(self, event):
        position = event.position().toPoint()
        if self.drag_mode is None:
            if self.grip_rect().contains(position):
                # the grip points into the picture: top left and bottom right resize along the same diagonal
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if self.corner() in ('TL', 'BR') else Qt.CursorShape.SizeBDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        container = self.parent()
        point = self.mapToParent(position)
        start = self.drag_start_geometry
        if not self.dragged and (point - self.drag_start_point).manhattanLength() < QApplication.startDragDistance():
            return
        self.dragged = True
        if self.drag_mode == 'move':
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            x = start.x() + point.x() - self.drag_start_point.x()
            y = start.y() + point.y() - self.drag_start_point.y()
            x = min(max(x, 0), max(container.width() - start.width(), 0))
            y = min(max(y, 0), max(container.height() - start.height(), 0))
            self.move(x, y)
            return
        # resize about the anchored corner: the one in the window's corner
        corner = self.corner()
        anchor_x = start.left() if corner[1] == 'L' else start.right()
        anchor_y = start.bottom() if corner[0] == 'B' else start.top()
        wanted = max(abs(point.x() - anchor_x), abs(point.y() - anchor_y) * self.camera_aspect)
        self.setGeometry(self.target_geometry(corner=corner, scale=wanted / max(container.width(), 1)))

    def mouseReleaseEvent(self, event):
        mode, dragged = self.drag_mode, self.dragged
        self.drag_mode = None
        self.dragged = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        container = self.parent()
        if not dragged or container is None:
            return
        settings = QSettings()
        geometry = self.geometry()
        if mode == 'move':
            center = geometry.center()
            corner = ('B' if center.y() >= container.height() / 2 else 'T') + ('L' if center.x() < container.width() / 2 else 'R')
            settings.setValue(MY_VIDEO_CORNER_KEY, corner)
        elif mode == 'resize' and container.width() > 0:
            settings.setValue(MY_VIDEO_SCALE_KEY, geometry.width() / container.width())
        self.layout_in_parent(animate=True)

    def mouseDoubleClickEvent(self, event):
        event.accept()                # not a full screen toggle


class RemoteVideoView(TrackedVideoSurface):
    """The main picture. Dragging it moves the window, a double click toggles full screen."""

    doubleClicked = pyqtSignal()

    def __init__(self, parent):
        super(RemoteVideoView, self).__init__(parent)
        self.interactive = False
        self.press_position = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press_position = event.position().toPoint()
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if self.press_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (event.position().toPoint() - self.press_position).manhattanLength() >= QApplication.startDragDistance():
                self.press_position = None
                window = self.window()
                if not window.isFullScreen() and window.windowHandle() is not None:
                    window.windowHandle().startSystemMove()

    def mouseReleaseEvent(self, event):
        self.press_position = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()


# Screenshots

class VideoScreenshot(object):
    def __init__(self, surface):
        self.surface = surface
        self.image = None

    @classmethod
    def filename_generator(cls):
        settings = BlinkSettings()
        name = os.path.join(settings.screenshots_directory.normalized, 'VideoCall-{:%Y%m%d-%H.%M.%S}'.format(datetime.now()))
        yield '%s.png' % name
        for x in count(1):
            yield "%s-%d.png" % (name, x)

    def capture(self):
        image = getattr(self.surface, '_image', None)
        if image is None:
            return False
        self.image = image.copy()
        settings = SIPSimpleSettings()
        if not settings.audio.silent:
            player = WavePlayer(SIPApplication.alert_audio_bridge.mixer, Resources.get('sounds/screenshot.wav'), volume=30)
            SIPApplication.alert_audio_bridge.add(player)
            player.start()
        return True

    @run_in_thread('file-io')
    def save(self, callback=None):
        filename = None
        if self.image is not None:
            filename = next(filename for filename in self.filename_generator() if not os.path.exists(filename))
            try:
                makedirs(os.path.dirname(filename))
                if not self.image.save(filename):
                    filename = None
            except OSError:
                filename = None
        if callback is not None:
            call_in_gui_thread(callback, filename)


# The window

@implementer(IObserver)
class VideoWindow(QWidget):
    closed = pyqtSignal(object)

    def __init__(self, blink_session):
        super(VideoWindow, self).__init__(None)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet('VideoWindow { background-color: #101010; }')
        self.setMinimumSize(240, 135)
        self.setMouseTracking(True)
        self.blink_session = blink_session
        self.connected = False            # the remote picture is in the main view
        self.swapped = False              # the local camera is in the main view and the remote in the thumbnail
        self.local_video_hidden = False
        self.aspect_ratio = None          # None follows the remote picture
        self.always_on_top = False
        self.ending = False
        self.received_video = False
        self._remote_producer = None

        self.video_view = RemoteVideoView(self)
        self.video_view.setMouseTracking(True)
        self.video_view.doubleClicked.connect(self.toggle_full_screen)
        self.my_video_view = LocalVideoView(self, framerate=None)
        self.my_video_view.setMouseTracking(True)
        self.my_video_view.hide()

        self.status_label = StatusLabel(self)
        self.call_bar = CallBar(self)
        self.my_video_view.bottom_obstacle = lambda: self.call_bar.geometry() if self.call_bar.isVisibleTo(self) and self.call_bar.shown else None
        self.toast = VideoToast(self)

        bar = self.call_bar
        self.mute_button = bar.add_segment('mic', translate('video_window', 'Mute'))
        self.camera_button = bar.add_segment('camera', translate('video_window', 'Camera'))
        self.camera_button.setToolTip(translate('video_window', 'Video and camera'))
        self.chat_button = bar.add_segment('chat', translate('video_window', 'Chat'))
        self.chat_button.setToolTip(translate('video_window', 'Open the MSRP session'))
        self.screenshot_button = bar.add_segment('screenshot', translate('video_window', 'Screenshot'))
        self.record_button = bar.add_segment('record', translate('video_window', 'Record'))
        self.record_button.setToolTip(translate('video_window', 'Start recording'))
        self.fullscreen_button = bar.add_segment('fullscreen', translate('video_window', 'Full'))
        self.fullscreen_button.setToolTip(translate('video_window', 'Full screen'))
        self.info_button = bar.add_segment('info', translate('video_window', 'Info'))
        self.info_button.setToolTip(translate('video_window', 'Show session information'))
        self.hangup_button = bar.add_segment('hangup', translate('video_window', 'End'), destructive=True)
        self.hangup_button.setToolTip(translate('video_window', 'Hang up'))

        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        self.camera_button.clicked.connect(self._SH_CameraButtonClicked)
        self.chat_button.clicked.connect(self._SH_ChatButtonClicked)
        self.screenshot_button.clicked.connect(self._SH_ScreenshotButtonClicked)
        self.record_button.clicked.connect(self._SH_RecordButtonClicked)
        self.fullscreen_button.clicked.connect(self.toggle_full_screen)
        self.info_button.clicked.connect(self._SH_InfoButtonClicked)
        self.hangup_button.clicked.connect(self._SH_HangupButtonClicked)

        for widget in (self.video_view, self.my_video_view, self.call_bar, self.toast, *self.call_bar.segments):
            widget.installEventFilter(self)
            widget.setMouseTracking(True)

        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(IDLE_TIME)
        self.idle_timer.timeout.connect(self._SH_IdleTimerTimeout)

        self.recording_timer = QTimer(self)
        self.recording_timer.setInterval(500)
        self.recording_timer.timeout.connect(self._SH_RecordingTimerTimeout)
        self.recording_blink = 0

        self.close_timer = QTimer(self)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self.close)

        # if a view has had no picture for a while after it got its producer, say so and log why
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setSingleShot(True)
        self.watchdog_timer.setInterval(5000)
        self.watchdog_timer.timeout.connect(self._SH_WatchdogTimeout)

        self.video_view.firstFrame.connect(self._SH_MainViewFirstFrame)
        self.my_video_view.firstFrame.connect(self._SH_ThumbnailFirstFrame)

        self._update_title()
        self.update_mute_button()
        self.update_record_button()
        self.update_buttons()

        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=blink_session)
        notification_center.add_observer(ObserverWeakrefProxy(self), name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoStreamRemoteFormatDidChange')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoStreamReceivedKeyFrame')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoDeviceDidChangeCamera')

        self.resize(self.size_hint_for_width(DEFAULT_WINDOW_WIDTH))
        self._center_on_screen()

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.blink_session)

    # geometry

    @property
    def video_aspect(self):
        if self.aspect_ratio is not None:
            return self.aspect_ratio
        producer = self._remote_producer if not self.swapped else None
        try:
            width, height = producer.framesize
            if width >= 16 and height >= 16:
                return width / height
        except (AttributeError, TypeError, ValueError):
            pass
        return 16 / 9

    def size_hint_for_width(self, width):
        width = max(width, self.minimumWidth())
        return QSize(width, max(int(ceil(width / self.video_aspect)), self.minimumHeight()))

    def _center_on_screen(self):
        blink = QApplication.instance()
        main_window = getattr(blink, 'main_window', None)
        screen = blink.screenAt(main_window.geometry().center()) if main_window is not None else None
        screen = screen or blink.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        size = self.size()
        if size.width() > area.width() * 0.8 or size.height() > area.height() * 0.8:
            self.resize(self.size_hint_for_width(int(min(area.width() * 0.8, area.height() * 0.8 * self.video_aspect))))
            size = self.size()
        self.move(area.center() - QPoint(size.width() // 2, size.height() // 2))

    def _fit_to_aspect(self):
        """Give the window the shape of the picture, keeping its width."""
        if self.isFullScreen() or self.isMaximized():
            return
        self.resize(self.size_hint_for_width(self.width()))

    def resizeEvent(self, event):
        super(VideoWindow, self).resizeEvent(event)
        self.video_view.setGeometry(self.rect())
        self.call_bar.tile()
        too_small = self.width() < self.call_bar.minimum_window_width()
        self.call_bar.setVisible(not too_small)
        self.my_video_view.layout_in_parent()
        if self.toast.isVisible():
            self.toast.tile()
        if self.status_label.isVisible():
            self.status_label.set_status(self.status_label.text())

    # chrome

    def show_controls(self):
        self.call_bar.fade_in()
        self.unsetCursor()
        self.video_view.unsetCursor()
        self.idle_timer.start()
        self.my_video_view.layout_in_parent(animate=True)

    def hide_controls(self):
        if self.camera_menu_open:
            return
        self.call_bar.fade_out()
        if self.isFullScreen():
            self.video_view.setCursor(Qt.CursorShape.BlankCursor)
        self.my_video_view.layout_in_parent(animate=True)

    camera_menu_open = False

    def _SH_IdleTimerTimeout(self):
        if self.call_bar.underMouse() and not self.isFullScreen():
            self.idle_timer.start()
            return
        self.hide_controls()

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.Enter, QEvent.Type.HoverMove):
            if not self.call_bar.shown:
                self.show_controls()
            else:
                self.idle_timer.start()
        return False

    def mouseMoveEvent(self, event):
        if not self.call_bar.shown:
            self.show_controls()
        else:
            self.idle_timer.start()
        super(VideoWindow, self).mouseMoveEvent(event)

    def enterEvent(self, event):
        self.show_controls()
        super(VideoWindow, self).enterEvent(event)

    def leaveEvent(self, event):
        if not self.isFullScreen():
            self.idle_timer.start(800)
        super(VideoWindow, self).leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.toggle_full_screen()
            else:
                self.remove_video()
        elif event.key() == Qt.Key.Key_F and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.toggle_full_screen()
        else:
            super(VideoWindow, self).keyPressEvent(event)

    def contextMenuEvent(self, event):
        session = self.blink_session
        if session is None:
            return
        menu = QMenu(self)
        connected = session.state == 'connected'
        menu.addAction(translate('video_window', 'Remove Video'), self.remove_video).setEnabled(connected and 'video' in session.streams)
        menu.addAction(translate('video_window', 'Hang Up'), self._SH_HangupButtonClicked)
        hold = menu.addAction(translate('video_window', 'Unhold') if session.local_hold else translate('video_window', 'Hold'), self._SH_HoldActionTriggered)
        hold.setEnabled(connected and 'audio' in session.streams)
        mute = menu.addAction(translate('video_window', 'Mute'), self._SH_MuteButtonClicked)
        mute.setCheckable(True)
        mute.setChecked(SIPSimpleSettings().audio.muted)
        menu.addSeparator()
        on_top = menu.addAction(translate('video_window', 'Always On Top'), self.toggle_always_on_top)
        on_top.setCheckable(True)
        on_top.setChecked(self.always_on_top)
        on_top.setEnabled(not self.isFullScreen())
        menu.addAction(translate('video_window', 'Exit Full Screen') if self.isFullScreen() else translate('video_window', 'Full Screen'), self.toggle_full_screen)
        menu.addSeparator()
        menu.addAction(translate('video_window', 'Screenshot'), self._SH_ScreenshotButtonClicked).setEnabled(self.main_view_has_picture)
        menu.addAction(translate('video_window', 'Open Screenshots Folder'), self.open_screenshots_folder)
        menu.addSeparator()
        menu.addAction(translate('video_window', 'Info'), self._SH_InfoButtonClicked).setEnabled(self.info_button.isEnabled())
        preview = menu.addAction(translate('video_window', 'Local Video'), self.toggle_local_video)
        preview.setCheckable(True)
        preview.setChecked(not self.local_video_hidden)
        preview.setEnabled(self.connected)
        self._exec_menu(menu, event.globalPos())

    def _exec_menu(self, menu, position):
        self.camera_menu_open = True
        self.idle_timer.stop()
        try:
            menu.exec(position)
        finally:
            self.camera_menu_open = False
            self.idle_timer.start()

    # window state

    def toggle_full_screen(self):
        if self.isFullScreen():
            self.showNormal()
            if self.always_on_top:
                self._apply_always_on_top()
        else:
            self.showFullScreen()
        self._update_fullscreen_button()
        self.show_controls()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_fullscreen_button()
            self.my_video_view.layout_in_parent()
        super(VideoWindow, self).changeEvent(event)

    def _update_fullscreen_button(self):
        if self.isFullScreen():
            self.fullscreen_button.configure('fullscreen.exit', translate('video_window', 'Exit'))
            self.fullscreen_button.setToolTip(translate('video_window', 'Exit full screen'))
        else:
            self.fullscreen_button.configure('fullscreen', translate('video_window', 'Full'))
            self.fullscreen_button.setToolTip(translate('video_window', 'Full screen'))

    def toggle_always_on_top(self):
        self.always_on_top = not self.always_on_top
        self._apply_always_on_top()

    def _apply_always_on_top(self):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.always_on_top)
        if visible:
            self.show()

    def toggle_local_video(self):
        self.local_video_hidden = not self.local_video_hidden
        self.my_video_view.setVisible(self.connected and not self.local_video_hidden)

    def _update_title(self):
        name = self.blink_session.contact.name if self.blink_session is not None and self.blink_session.contact is not None else ''
        self.setWindowTitle(translate('video_window', 'Video with %s') % name)

    # buttons

    def update_mute_button(self):
        if SIPSimpleSettings().audio.muted:
            self.mute_button.configure('mic.slash', translate('video_window', 'Unmute'), tint=RED)
        else:
            self.mute_button.configure('mic', translate('video_window', 'Mute'))

    def update_record_button(self):
        session = self.blink_session
        recording = session is not None and session.recording
        if recording:
            if not self.recording_timer.isActive():
                self.recording_blink = 0
                self.recording_timer.start()
            tint = RED if self.recording_blink == 0 else white(0.5)
            self.record_button.configure('record.active', translate('video_window', 'Stop'), tint=tint)
            self.record_button.setToolTip(translate('video_window', 'Stop recording'))
        else:
            self.recording_timer.stop()
            self.record_button.configure('record', translate('video_window', 'Record'))
            self.record_button.setToolTip(translate('video_window', 'Start recording'))

    def _SH_RecordingTimerTimeout(self):
        self.recording_blink = (self.recording_blink + 1) % 2
        self.update_record_button()

    def update_buttons(self):
        session = self.blink_session
        if session is None:
            return
        connected = session.state == 'connected'
        has_audio = 'audio' in session.streams
        self.mute_button.setVisible(has_audio)
        self.record_button.setVisible(has_audio)
        self.record_button.setEnabled(connected)
        self.chat_button.setEnabled(connected or 'chat' in session.streams)
        self.screenshot_button.setEnabled(self.main_view_has_picture)
        self.info_button.setVisible(session.items.audio is not None)
        self.info_button.setEnabled(session.items.audio is not None)
        self.call_bar.tile()

    @property
    def main_view_has_picture(self):
        return getattr(self.video_view, '_image', None) is not None

    def _SH_MuteButtonClicked(self):
        settings = SIPSimpleSettings()
        settings.audio.muted = not settings.audio.muted
        settings.save()

    def _SH_HoldActionTriggered(self):
        if self.blink_session.local_hold:
            self.blink_session.unhold()
        else:
            self.blink_session.hold()

    def _SH_RecordButtonClicked(self):
        session = self.blink_session
        if session.recording:
            session.stop_recording()
        else:
            session.start_recording()
        self.update_record_button()

    def _SH_ChatButtonClicked(self):
        """Open this call in the MSRP sessions window, adding a chat stream if it has none."""
        from blink.sessions import StreamDescription
        session = self.blink_session
        if 'chat' not in session.streams:
            if session.state != 'connected':
                return
            try:
                session.add_stream(StreamDescription('chat'))
            except RuntimeError:
                return
        if self.isFullScreen():
            self.toggle_full_screen()
        NotificationCenter().post_notification('BlinkSessionIsSelected', sender=session)

    def _SH_InfoButtonClicked(self):
        session = self.blink_session
        item = session.items.audio
        if item is None:
            return
        from blink.widgets.buttons import SwitchViewButton
        main_window = QApplication.instance().main_window
        if self.isFullScreen():
            self.toggle_full_screen()
        on_screen = main_window.isVisible() and not main_window.isMinimized() and main_window.main_view.currentWidget() is main_window.sessions_panel
        session_list = main_window.session_list
        # the info is under the call in the audio panel: go there, and only hide it if it was already in view
        main_window.switch_view_button.view = SwitchViewButton.SessionView
        main_window.main_view.setCurrentWidget(main_window.sessions_panel)
        if main_window.isMinimized():
            main_window.showNormal()
        else:
            main_window.show()
        main_window.raise_()
        main_window.activateWindow()
        if on_screen and session_list.info_session is item:
            session_list.hide_session_info()
        else:
            session_list.show_session_info(item)

    def _SH_HangupButtonClicked(self):
        if self.isFullScreen():
            self.showNormal()
        self.hide()
        self.blink_session.end()

    def _SH_ScreenshotButtonClicked(self):
        screenshot = VideoScreenshot(self.video_view)
        if not screenshot.capture():
            self.toast.show_message(translate('video_window', 'No picture to take a screenshot of'))
            return
        screenshot.save(callback=self._screenshot_saved)

    def _screenshot_saved(self, filename):
        if self.blink_session is None:
            return
        if filename is not None:
            ActivityLog().info(f'[video] Screenshot saved in {filename}')
            self._file_screenshot(filename)
            self.toast.show_message(translate('video_window', 'Screenshot saved'), translate('video_window', 'Show in Folder'), lambda: self.open_screenshots_folder())
        else:
            self.toast.show_message(translate('video_window', 'Screenshot failed'))

    def _file_screenshot(self, filename):
        """Put the screenshot in the conversation with the other party, and on our own devices
        when the account speaks SylkServer's API. Never sent to the other party.

        Filed as a call recording is: a copy where a received file of that conversation is kept
        (file_transfers/<account>/<peer>/<id>/), as an outgoing picture from us to us.
        """
        import shutil
        import uuid
        from datetime import timezone
        from blink.file_transfer import transfer_folder
        from blink.history import MessageHistory, conversation_key
        from blink.messages import MessageManager
        from blink.resources import ApplicationData
        session = self.blink_session
        account = session.account
        if account is None or session.contact_uri is None:
            return
        party_uri = str(session.contact_uri.uri)
        display_name = session.contact.name if session.contact is not None else ''
        key = conversation_key(party_uri, account)
        transfer_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        # a neutral name: it goes to the server and into the file list of every device
        name = 'screenshot-%s.png' % now.astimezone().strftime('%Y%m%d-%H%M%S')
        directory = transfer_folder(ApplicationData.get('file_transfers'), account.id, key, transfer_id)
        path = os.path.join(directory, name)
        try:
            os.makedirs(directory, exist_ok=True)
            shutil.copyfile(filename, path)
        except OSError as e:
            ActivityLog().error(f'[video] Cannot put the screenshot in the conversation with {key}: {e}')
            return
        MessageHistory().add_call_screenshot(path, transfer_id, key, account, party_uri, display_name, now.replace(tzinfo=None))
        MessageManager().share_with_own_devices(account, path, transfer_id, party_uri, display_name, name)

    def open_screenshots_folder(self):
        directory = BlinkSettings().screenshots_directory.normalized
        QDesktopServices.openUrl(QUrl.fromLocalFile(directory))

    def _SH_CameraButtonClicked(self):
        """What the video does, then which camera. Offered with a single camera too."""
        session = self.blink_session
        menu = QMenu(self)
        connected = self.connected and session.streams.get('video') is not None
        menu.addAction(translate('video_window', 'Show Preview') if self.local_video_hidden else translate('video_window', 'Hide Preview'), self.toggle_local_video).setEnabled(connected)
        swap = menu.addAction(translate('video_window', 'Swap Video'), self.swap_video)
        swap.setCheckable(True)
        swap.setChecked(self.swapped)
        swap.setEnabled(connected)
        menu.addAction(translate('video_window', 'Stop Video'), self.remove_video).setEnabled(session.state == 'connected' and 'video' in session.streams)

        aspect_menu = menu.addMenu(translate('video_window', 'Aspect Ratio'))
        group = QActionGroup(aspect_menu)
        for ratio, title in [(None, translate('video_window', 'Original'))] + ASPECT_RATIOS:
            action = aspect_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(ratio == self.aspect_ratio if ratio is None or self.aspect_ratio is None else abs(ratio - self.aspect_ratio) < 0.005)
            action.triggered.connect(lambda checked, ratio=ratio: self.set_aspect_ratio(ratio))
            group.addAction(action)

        menu.addSeparator()
        try:
            cameras = [device for device in SIPApplication.engine.video_devices if device not in (None, 'system_default')]
        except AttributeError:
            cameras = []
        settings = SIPSimpleSettings()
        try:
            current = SIPApplication.video_device.real_name
        except AttributeError:
            current = settings.video.device
        if not cameras:
            menu.addAction(translate('video_window', 'No Camera')).setEnabled(False)
        camera_group = QActionGroup(menu)
        for camera in sorted(cameras, key=str.lower):
            action = menu.addAction(camera)
            action.setCheckable(True)
            action.setChecked(camera == current)
            action.triggered.connect(lambda checked, camera=camera: self.select_camera(camera))
            camera_group.addAction(action)

        button = self.camera_button
        position = button.mapToGlobal(QPoint(0, 0))
        size = menu.sizeHint()
        # above the bar, it sits at the bottom of the window
        self._exec_menu(menu, QPoint(position.x(), position.y() - size.height() - 4))

    @staticmethod
    def select_camera(camera):
        settings = SIPSimpleSettings()
        if settings.video.device != camera:
            ActivityLog().info(f'[video] Switching to the {camera} camera')
            settings.video.device = camera
            settings.save()

    def set_aspect_ratio(self, ratio):
        self.aspect_ratio = ratio
        self._fit_to_aspect()

    # video

    @property
    def local_producer(self):
        try:
            return SIPApplication.video_device.producer
        except AttributeError:
            return None

    def show_preview_layout(self):
        """Before the call connects: the local camera fills the window, nothing to compare it with."""
        self.connected = False
        self.swapped = False
        self.my_video_view.hide()
        self.my_video_view.producer = None
        self.video_view.mirror = True
        self.video_view.producer = self.local_producer
        self.log('Preview: camera %s in the main view' % self.describe_camera())
        self.status_label.set_status(translate('video_window', 'Connecting...'))
        self.watchdog_timer.start()

    def show_connected_layout(self, stream):
        """The remote picture in the window, the local camera in the corner.

        Both views are detached first and attached after the event loop has
        drained: switching a producer in place races the converter thread.
        """
        self._remote_producer = stream.producer
        first_time = not self.connected
        self.connected = True
        if not first_time:
            return
        self.video_view.producer = None
        self.my_video_view.producer = None
        if not self.received_video:
            self.status_label.set_status(translate('video_window', 'Waiting for remote video...'))
        QTimer.singleShot(250, self._attach_producers)

    def _attach_producers(self):
        if self.blink_session is None or not self.connected:
            return
        remote, local = self._remote_producer, self.local_producer
        main, thumb = (local, remote) if self.swapped else (remote, local)
        self.video_view.mirror = self.swapped
        self.my_video_view.mirror = not self.swapped
        self.video_view.producer = main
        self.my_video_view.producer = thumb
        self.my_video_view.set_placeholder(translate('video_window', 'Starting camera...') if not self.swapped else translate('video_window', 'Waiting for remote video...'))
        self.log('Attached: main view <- %s %s, thumbnail <- %s %s' % ('camera' if self.swapped else 'remote', describe_producer(main),
                                                                      'remote' if self.swapped else 'camera', describe_producer(thumb)))
        if thumb is None or main is None:
            self.log('No producer for the %s: %s' % ('thumbnail' if thumb is None else 'main view', self.describe_camera()))
        self.watchdog_timer.start()
        self.my_video_view.setVisible(not self.local_video_hidden)
        self.my_video_view.raise_()
        self.call_bar.raise_()
        self.toast.raise_()
        self.my_video_view.layout_in_parent()
        self._fit_to_aspect()
        self.update_buttons()

    # diagnostics

    def log(self, text):
        name = self.blink_session.contact_uri.uri if self.blink_session is not None and self.blink_session.contact_uri is not None else '?'
        ActivityLog().info(f'[video] {name}: {text}')

    @staticmethod
    def describe_camera():
        """What the SDK says about the camera, for the Activity log."""
        settings = SIPSimpleSettings()
        device = getattr(SIPApplication, 'video_device', None)
        parts = ['setting=%r' % settings.video.device]
        if device is None:
            parts.append('video_device=None')
        else:
            for attribute in ('name', 'real_name'):
                parts.append('%s=%r' % (attribute, getattr(device, attribute, '<missing>')))
            parts.append('producer=%s' % describe_producer(getattr(device, 'producer', None)))
        try:
            parts.append('devices=%r' % list(SIPApplication.engine.video_devices))
        except Exception:
            pass
        return ' '.join(parts)

    def _SH_MainViewFirstFrame(self, width, height):
        if self.blink_session is None:
            return
        local = not self.connected or self.swapped
        self.log('First %s frame in the main view: %dx%d' % ('camera' if local else 'remote', width, height))
        if not local:
            self._remote_video_arrived()
        self.screenshot_button.setEnabled(True)

    def _SH_ThumbnailFirstFrame(self, width, height):
        if self.blink_session is None:
            return
        self.log('First %s frame in the thumbnail: %dx%d' % ('remote' if self.swapped else 'camera', width, height))
        self.my_video_view.set_placeholder('')
        self.my_video_view.layout_in_parent()     # the camera's real shape is known now

    def _remote_video_arrived(self):
        if self.received_video:
            return
        self.received_video = True
        if self.blink_session is not None and not self.blink_session.on_hold:
            self.status_label.set_status(None)
        self.update_buttons()
        self._fit_to_aspect()

    def _SH_WatchdogTimeout(self):
        if self.blink_session is None or self.ending:
            return
        main, thumb = self.video_view, self.my_video_view
        if not self.connected:
            if main.frame_count == 0:
                self.log('No picture from the camera after 5s: %s' % self.describe_camera())
            return
        camera_view, remote_view = (main, thumb) if self.swapped else (thumb, main)
        if camera_view.frame_count == 0:
            self.log('No picture from the camera after 5s: %s' % self.describe_camera())
            if camera_view is thumb:
                thumb.set_placeholder(translate('video_window', 'No picture from the camera'))
        if remote_view.frame_count == 0:
            video_stream = self.blink_session.streams.get('video')
            self.log('No remote picture after 5s: stream=%s producer=%s' % (video_stream, describe_producer(getattr(video_stream, 'producer', None))))
        else:
            self.log('Frames so far: main view %d, thumbnail %d' % (main.frame_count, thumb.frame_count))

    def swap_video(self):
        if not self.connected or self._remote_producer is None:
            return
        self.swapped = not self.swapped
        ActivityLog().info('[video] %s' % ('Video swapped: local camera in the main view' if self.swapped else 'Video restored: remote in the main view'))
        self.video_view.producer = None
        self.my_video_view.producer = None
        self.video_view._image = None
        self.my_video_view._image = None
        QTimer.singleShot(250, self._attach_producers)

    def remove_video(self):
        session = self.blink_session
        if session is None:
            return
        video_stream = session.streams.get('video')
        if session.state == 'connected' and video_stream is not None and len(session.streams) > 1:
            session.remove_stream(video_stream)
        else:
            session.end()

    def _release_video(self):
        for view in (self.video_view, self.my_video_view):
            try:
                view.producer = None
            except Exception:
                pass
            view._image = None

    def closeEvent(self, event):
        """Closing the window stops the video; the call goes on if it has anything else."""
        if not self.ending and self.blink_session is not None:
            self.ending = True
            self._release_video()
            self.remove_video()
        self._teardown()
        super(VideoWindow, self).closeEvent(event)

    def _teardown(self):
        if self.blink_session is None:
            return
        self.idle_timer.stop()
        self.recording_timer.stop()
        self.close_timer.stop()
        self.watchdog_timer.stop()
        self._release_video()
        for view in (self.video_view, self.my_video_view):
            try:
                view.stop()
            except Exception:
                pass
        notification_center = NotificationCenter()
        for kw in (dict(sender=self.blink_session), dict(name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings()),
                   dict(name='VideoStreamRemoteFormatDidChange'), dict(name='VideoStreamReceivedKeyFrame'), dict(name='VideoDeviceDidChangeCamera')):
            try:
                notification_center.discard_observer(ObserverWeakrefProxy(self), **kw)
            except Exception:
                pass
        blink_session, self.blink_session = self.blink_session, None
        self.closed.emit(blink_session)

    def end_video(self, status=None, delay=0):
        """The video is over: say why, if there is something to say, and go."""
        if self.ending:
            return
        self.ending = True
        self._release_video()
        self.my_video_view.hide()
        if status and delay and self.isVisible():
            self.status_label.set_status(status)
            self.close_timer.start(int(delay * 1000))
        else:
            self.close()

    # notifications

    @run_in_gui_thread
    def handle_notification(self, notification):
        if self.blink_session is None:
            return
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidConnect(self, notification):
        video_stream = notification.sender.streams.get('video')
        if video_stream is None:
            self.end_video(translate('video_window', 'Video was not accepted'), delay=2)
            return
        self.show_connected_layout(video_stream)
        self.update_buttons()

    def _NH_BlinkSessionDidAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.show_connected_layout(notification.data.stream)
        self.update_buttons()

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.end_video(translate('video_window', 'Video was not accepted'), delay=2)
        else:
            self.update_buttons()

    def _NH_BlinkSessionWillRemoveStream(self, notification):
        if notification.data.stream.type == 'video':
            self._release_video()

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'video':
            self.end_video()
        else:
            self.update_buttons()

    def _NH_BlinkSessionDidChangeState(self, notification):
        self.update_buttons()

    def _NH_BlinkSessionDidEnd(self, notification):
        self.end_video(translate('sessions', notification.data.reason) if getattr(notification.data, 'reason', None) else None, delay=2)

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.ending = True
        self.close()

    def _NH_BlinkSessionWillReinitialize(self, notification):
        self.ending = True
        self.close()

    def _NH_BlinkSessionContactDidChange(self, notification):
        self._update_title()

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        if notification.data.local_hold:
            self.status_label.set_status(translate('video_window', 'On hold'))
        elif notification.data.remote_hold:
            self.status_label.set_status(translate('video_window', 'Held by remote'))
        elif self.connected and self.received_video:
            self.status_label.set_status(None)

    def _NH_BlinkSessionDidChangeRecordingState(self, notification):
        self.update_record_button()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'audio.muted' in notification.data.modified:
            self.update_mute_button()

    def _NH_VideoStreamRemoteFormatDidChange(self, notification):
        if getattr(notification.sender, 'blink_session', None) is self.blink_session and self.aspect_ratio is None:
            self._fit_to_aspect()

    def _NH_VideoStreamReceivedKeyFrame(self, notification):
        if getattr(notification.sender, 'blink_session', None) is self.blink_session:
            self._remote_video_arrived()

    def _NH_VideoDeviceDidChangeCamera(self, notification):
        new_camera = notification.data.new_camera
        self.log('Camera changed: %s' % describe_producer(new_camera))
        if not self.connected:
            self.video_view.producer = new_camera
        elif self.swapped:
            self.video_view.producer = new_camera
        else:
            self.my_video_view.producer = new_camera
            self.my_video_view.set_placeholder(translate('video_window', 'Starting camera...'))
            self.my_video_view.layout_in_parent()
        self.watchdog_timer.start()


@implementer(IObserver)
class VideoWindowManager(object):
    """Opens a video window for every call that gets video, and forgets it when it closes."""

    def __init__(self):
        self.windows = {}
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkSessionWillConnect')
        notification_center.add_observer(self, name='BlinkSessionDidConnect')
        notification_center.add_observer(self, name='BlinkSessionWillAddStream')

    def window_for(self, blink_session):
        return self.windows.get(blink_session)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionWillConnect(self, notification):
        # an outgoing call: the local camera shows until it connects
        if 'video' in notification.sender.streams:
            self._open(notification.sender)

    def _NH_BlinkSessionDidConnect(self, notification):
        # an incoming call that was answered goes straight to connected
        video_stream = notification.sender.streams.get('video')
        if video_stream is not None:
            self._open(notification.sender, video_stream)

    def _NH_BlinkSessionWillAddStream(self, notification):
        # video added to a call, by us or by the other party
        if notification.data.stream.type == 'video':
            self._open(notification.sender)

    def _open(self, blink_session, connected_stream=None):
        window = self.windows.get(blink_session)
        if window is not None and not window.ending:
            if connected_stream is not None:
                window.show_connected_layout(connected_stream)
            if not window.isVisible():
                window.show()
            return
        window = VideoWindow(blink_session)
        window.closed.connect(self._SH_WindowClosed)
        self.windows[blink_session] = window
        if connected_stream is not None:
            window.show_connected_layout(connected_stream)
        else:
            window.show_preview_layout()
        window.show()
        window.raise_()
        window.activateWindow()
        window.show_controls()

    def _SH_WindowClosed(self, blink_session):
        window = self.windows.get(blink_session)
        if window is not None and window.blink_session is None:
            del self.windows[blink_session]
