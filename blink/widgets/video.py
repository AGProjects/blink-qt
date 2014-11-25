# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from __future__ import division


__all__ = ['VideoSurface']


from PyQt4.QtCore import Qt, QMetaObject, QPoint, QRect, QTimer, pyqtSignal
from PyQt4.QtGui  import QColor, QCursor, QIcon, QImage, QPainter, QPixmap, QTransform, QWidget

from application.python.types import MarkerType
from math import ceil
from operator import truediv
from sipsimple.core import FrameBufferVideoRenderer

from blink.resources import Resources


class Container(object): pass
class Cursors(Container): pass


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
    class TopLeftCorner:     __metaclass__ = MarkerType
    class TopRightCorner:    __metaclass__ = MarkerType
    class BottomLeftCorner:  __metaclass__ = MarkerType
    class BottomRightCorner: __metaclass__ = MarkerType

    adjusted = pyqtSignal(QRect, QRect) # the widget was adjusted by the user (if interactive)

    interactive = False  # if the widget can be interacted with (moved, resized)
    mirror = False       # mirror the image horizontally

    def __init__(self, parent=None, framerate=None):
        super(VideoSurface, self).__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.cursors = Cursors()
        self.cursors.resize_top    = QCursor(QIcon(Resources.get('icons/resize-top.svg')).pixmap(16),    hotX=8,  hotY=0)
        self.cursors.resize_bottom = QCursor(QIcon(Resources.get('icons/resize-bottom.svg')).pixmap(16), hotX=8,  hotY=16)
        if framerate is not None:
            self._clock = QTimer()
            self._clock.setInterval(1000/framerate)
            self._clock.timeout.connect(self.update)
        else:
            self._clock = None
        self._interaction = InteractionState()
        self._image = None

    def __getattr__(self, name):
        if name == '_renderer':
            return self.__dict__.setdefault(name, FrameBufferVideoRenderer(self._handle_frame))
        raise AttributeError("'%s' object has no attribute '%s'" % (self.__class__.__name__, name))

    def _get_producer(self):
        return self._renderer.producer

    def _set_producer(self, producer):
        if self._clock is not None:
            self._clock.stop()
        self._renderer.producer = producer
        if self._clock is not None and producer is not None:
            self._clock.start()

    producer = property(_get_producer, _set_producer)
    del _get_producer, _set_producer

    @property
    def aspect(self):
        producer = self._renderer.producer
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
        self._renderer.close()
        del self._renderer

    def _handle_frame(self, frame):
        self._image = QImage(frame.data, frame.width, frame.height, QImage.Format_ARGB32)
        if self._clock is None:
            QMetaObject.invokeMethod(self, 'update', Qt.QueuedConnection)

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
                painter.drawPixmap(rect, QPixmap.fromImage(image.transformed(fast_scaler)).scaledToHeight(self.height(), Qt.SmoothTransformation), rect)
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
        if self.interactive and event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier:
            if self.rect().adjusted(0, 10, 0, -10).contains(event.pos()):
                self._interaction.moving = True
            else:
                self._interaction.resizing = True
                event_x = event.x()
                event_y = event.y()
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
            self._interaction.mouse_last_position = event.globalPos()
            self._interaction.initial_geometry = self.geometry()

    def mouseReleaseEvent(self, event):
        if self._interaction.active and self._interaction.initial_geometry != self.geometry():
            self.adjusted.emit(self._interaction.initial_geometry, self.geometry())
        self._interaction.clear()

    def mouseMoveEvent(self, event):
        if self._interaction.moving:
            mouse_position = event.globalPos()
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
            mouse_position = event.globalPos()
            delta_y = mouse_position.y() - self._interaction.mouse_last_position.y()
            geometry = self.geometry()

            if self._interaction.resize_corner is self.TopLeftCorner:
                delta_x = -(self.width_for_height(geometry.height() - delta_y) - geometry.width())
                geometry.setTopLeft(geometry.topLeft() + QPoint(delta_x, delta_y))
            elif self._interaction.resize_corner is self.TopRightCorner:
                delta_x =  (self.width_for_height(geometry.height() - delta_y) - geometry.width())
                geometry.setTopRight(geometry.topRight() + QPoint(delta_x, delta_y))
            elif self._interaction.resize_corner is self.BottomLeftCorner:
                delta_x = -(self.width_for_height(geometry.height() + delta_y) - geometry.width())
                geometry.setBottomLeft(geometry.bottomLeft() + QPoint(delta_x, delta_y))
            else:
                delta_x =  (self.width_for_height(geometry.height() + delta_y) - geometry.width())
                geometry.setBottomRight(geometry.bottomRight() + QPoint(delta_x, delta_y))

            if self.minimumHeight() <= geometry.height() <= self.maximumHeight() and (self.parent() is None or self.parent().rect().contains(geometry)):
                self.setGeometry(geometry)
                self._interaction.mouse_last_position = mouse_position
        elif self.interactive:
            mouse_position = event.pos()
            topbar_rect = QRect(0, 0, self.width(), 10)

            if self.rect().adjusted(0, 10, 0, -10).contains(mouse_position):
                self.setCursor(Qt.ArrowCursor)
            elif topbar_rect.contains(mouse_position):
                self.setCursor(self.cursors.resize_top)
            else:
                self.setCursor(self.cursors.resize_bottom)

    def closeEvent(self, event):
        super(VideoSurface, self).closeEvent(event)
        self.stop()


