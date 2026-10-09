
import platform
from math import ceil
from operator import truediv

from application.python.types import MarkerType
from PyQt6.QtCore import QMetaObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QIcon, QImage, QPainter, QPixmap,
                         QTransform)
from PyQt6.QtWidgets import QWidget
from sipsimple.core import FrameBufferVideoRenderer, VideoCamera

from blink import video_frames
from blink.resources import Resources

__all__ = ['VideoSurface']

if platform.system() == 'Darwin':
    import numpy as np


class Container(object):
    pass


class InteractionState(object):
    def __init__(self):
        self.moving = False
        self.resizing = False
        self.resize_corner = None
        self.mouse_last_position = None
        self.initial_geometry = None

    @property
    def active(self):
        return self.moving or self.resizing

    def clear(self):
        self.__init__()


class VideoSurface(QWidget):
    class TopLeftCorner(metaclass=MarkerType):     pass
    class TopRightCorner(metaclass=MarkerType):    pass
    class BottomLeftCorner(metaclass=MarkerType):  pass
    class BottomRightCorner(metaclass=MarkerType): pass

    adjusted = pyqtSignal(QRect, QRect)  # the widget was adjusted by the user (if interactive)

    interactive = False  # if the widget can be interacted with (moved, resized)
    mirror = False       # mirror the image horizontally

    def __init__(self, parent=None, framerate=None):
        super(VideoSurface, self).__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.cursors = Container()
        self.cursors.resize_top    = self._build_cursor('icons/resize-top.svg',    hot_x=8, hot_y=0, fallback=Qt.CursorShape.SizeVerCursor)
        self.cursors.resize_bottom = self._build_cursor('icons/resize-bottom.svg', hot_x=8, hot_y=0, fallback=Qt.CursorShape.SizeVerCursor)
        if framerate is not None:
            self._clock = QTimer()
            self._clock.setInterval(int(1000/framerate))
            self._clock.timeout.connect(self.update)
        else:
            self._clock = None
        self._interaction = InteractionState()
        self._image = None
        # A remote picture is read through the renderer video_frames shares with the call
        # recorder (one consumer per remote stream); the camera takes any number of renderers,
        # so a view of it keeps its own.
        self._producer = None
        self._subscription = None
        self._renderer = None

    @staticmethod
    def _build_cursor(icon_path, hot_x, hot_y, fallback):
        # QIcon.pixmap() can return a null pixmap when the SVG icon engine is
        # unavailable (e.g. the qsvgicon plugin is missing). Constructing a
        # QCursor from a null pixmap triggers "Cannot create bitmap cursor;
        # invalid bitmap(s)" and yields an unusable cursor, so fall back to a
        # standard cursor shape in that case.
        pixmap = QIcon(Resources.get(icon_path)).pixmap(16, 16)
        if pixmap.isNull():
            return QCursor(fallback)
        return QCursor(pixmap, hotX=hot_x, hotY=hot_y)

    def _get_producer(self):
        return self._producer

    def _set_producer(self, producer):
        if self._clock is not None:
            self._clock.stop()
        if self._subscription is not None:
            self._subscription.release()
            self._subscription = None
        if producer is not None and not isinstance(producer, VideoCamera):
            if self._renderer is not None and self._renderer.producer is not None:
                self._renderer.producer = None
            self._subscription = video_frames.subscribe(producer, self._handle_frame)
            self._producer = producer if self._subscription is not None else None
        else:
            if producer is not None and self._renderer is None:
                self._renderer = FrameBufferVideoRenderer(self._handle_frame)
            if self._renderer is not None:
                self._renderer.producer = producer
            self._producer = producer
        if self._clock is not None and self._producer is not None:
            self._clock.start()

    producer = property(_get_producer, _set_producer)
    del _get_producer, _set_producer

    @property
    def aspect(self):
        producer = self._producer
        return truediv(*producer.framesize) if producer is not None else 16/9

    def width_for_height(self, height):
        return int(ceil(height * self.aspect))

    def height_for_width(self, width):
        return int(ceil(width / self.aspect))

    def heightForWidth(self, width):
        return int(ceil(width * 9/16))

    def stop(self):
        if self._clock is not None:
            self._clock.stop()
        if self._subscription is not None:
            self._subscription.release()
            self._subscription = None
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None
        self._producer = None

    def convert_argb_to_bgra(self, data: bytes, width: int, height: int) -> bytes:
        expected_size = width * height * 4
        actual_size = len(data)

        if actual_size < expected_size:
            raise ValueError(f"Buffer too small: got {actual_size} bytes, expected {expected_size}")
        elif actual_size > expected_size:
            data = data[:expected_size]  # Trim the buffer if too large

        arr = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 4))
        bgra = arr[..., [3, 2, 1, 0]]
        return bgra.tobytes()

    def _handle_frame(self, frame):
        if platform.system() == 'Darwin':
            data = self.convert_argb_to_bgra(frame.data, frame.width, frame.height)
        else:
            data = frame.data
        self._image = QImage(data, frame.width, frame.height, frame.width * 4, QImage.Format.Format_ARGB32).copy()
        if self._clock is None:
            QMetaObject.invokeMethod(self, 'update', Qt.ConnectionType.QueuedConnection)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#101010'))

        image = self._image

        if image is not None:
            if self.height() < 240:
                fast_scaler = QTransform()
                scale = 297/image.height()
                if self.mirror:
                    fast_scaler.scale(-scale, scale)
                else:
                    fast_scaler.scale(scale, scale)
                rect = event.rect()
                painter.drawPixmap(rect, QPixmap.fromImage(image.transformed(fast_scaler)).scaledToHeight(self.height(), Qt.TransformationMode.SmoothTransformation), rect)
            else:
                transform = QTransform()
                scale = min(self.width()/image.width(), self.height()/image.height())
                if self.mirror:
                    transform.translate((self.width() + image.width()*scale)/2, (self.height() - image.height()*scale)/2)
                    transform.scale(-scale, scale)
                else:
                    transform.translate((self.width() - image.width()*scale)/2, (self.height() - image.height()*scale)/2)
                    transform.scale(scale, scale)

                inverse_transform, invertible = transform.inverted()
                rect = inverse_transform.mapRect(event.rect()).adjusted(-1, -1, 1, 1).intersected(image.rect())

                painter.setTransform(transform)

                if self.height() > 400:
                    painter.drawPixmap(rect, QPixmap.fromImage(image), rect)
                else:
                    painter.drawImage(rect, image, rect)

        painter.end()

    def mousePressEvent(self, event):
        if self.interactive and event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            if self.rect().adjusted(0, 10, 0, -10).contains(event.position().toPoint()):
                self._interaction.moving = True
            else:
                self._interaction.resizing = True
                event_x = event.position().x()
                event_y = event.position().y()
                half_width = self.width() / 2
                half_height = self.height() / 2
                if event_x < half_width and event_y < half_height:
                    self._interaction.resize_corner = self.TopLeftCorner
                elif event_x >= half_width and event_y < half_height:
                    self._interaction.resize_corner = self.TopRightCorner
                elif event_x < half_width and event_y >= half_height:
                    self._interaction.resize_corner = self.BottomLeftCorner
                else:
                    self._interaction.resize_corner = self.BottomRightCorner
            self._interaction.mouse_last_position = event.globalPosition().toPoint()
            self._interaction.initial_geometry = self.geometry()

    def mouseReleaseEvent(self, event):
        if self._interaction.active and self._interaction.initial_geometry != self.geometry():
            self.adjusted.emit(self._interaction.initial_geometry, self.geometry())
        self._interaction.clear()

    def mouseMoveEvent(self, event):
        if self._interaction.moving:
            mouse_position = event.globalPosition().toPoint()
            offset = mouse_position - self._interaction.mouse_last_position
            if self.parent() is not None:
                parent_rect = self.parent().rect()
                old_geometry = self.geometry()
                new_geometry = old_geometry.translated(offset)
                if new_geometry.left() < 0:
                    new_geometry.moveLeft(0)
                if new_geometry.top() < 0:
                    new_geometry.moveTop(0)
                if new_geometry.right() > parent_rect.right():
                    new_geometry.moveRight(parent_rect.right())
                if new_geometry.bottom() > parent_rect.bottom():
                    new_geometry.moveBottom(parent_rect.bottom())
                offset = new_geometry.topLeft() - old_geometry.topLeft()
            self.move(self.pos() + offset)
            self._interaction.mouse_last_position += offset
        elif self._interaction.resizing:
            mouse_position = event.globalPosition().toPoint()
            delta_y = mouse_position.y() - self._interaction.mouse_last_position.y()
            geometry = self.geometry()

            if self._interaction.resize_corner is self.TopLeftCorner:
                delta_x = -(self.width_for_height(geometry.height() - delta_y) - geometry.width())
                geometry.setTopLeft(geometry.topLeft() + QPoint(delta_x, delta_y))
            elif self._interaction.resize_corner is self.TopRightCorner:
                delta_x = +(self.width_for_height(geometry.height() - delta_y) - geometry.width())
                geometry.setTopRight(geometry.topRight() + QPoint(delta_x, delta_y))
            elif self._interaction.resize_corner is self.BottomLeftCorner:
                delta_x = -(self.width_for_height(geometry.height() + delta_y) - geometry.width())
                geometry.setBottomLeft(geometry.bottomLeft() + QPoint(delta_x, delta_y))
            else:
                delta_x = +(self.width_for_height(geometry.height() + delta_y) - geometry.width())
                geometry.setBottomRight(geometry.bottomRight() + QPoint(delta_x, delta_y))

            if self.minimumHeight() <= geometry.height() <= self.maximumHeight() and (self.parent() is None or self.parent().rect().contains(geometry)):
                self.setGeometry(geometry)
                self._interaction.mouse_last_position = mouse_position
        elif self.interactive:
            mouse_position = event.position().toPoint()
            topbar_rect = QRect(0, 0, self.width(), 10)

            if self.rect().adjusted(0, 10, 0, -10).contains(mouse_position):
                self.setCursor(Qt.CursorShape.ArrowCursor)

            elif topbar_rect.contains(mouse_position):
                self.setCursor(self.cursors.resize_top)
            else:
                self.setCursor(self.cursors.resize_bottom)

    def closeEvent(self, event):
        super(VideoSurface, self).closeEvent(event)
        self.stop()

