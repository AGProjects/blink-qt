# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['BackgroundFrame']

from PyQt4.QtCore import Qt, QEvent, QPoint, QRect, QSize
from PyQt4.QtGui import QColor, QFrame, QPainter, QPixmap

from blink.resources import Resources
from blink.widgets.util import QtDynamicProperty


class BackgroundFrame(QFrame):
    backgroundColor = QtDynamicProperty('backgroundColor', unicode)
    backgroundImage = QtDynamicProperty('backgroundImage', unicode)
    imageGeometry   = QtDynamicProperty('imageGeometry', QRect)

    def __init__(self, parent=None):
        super(BackgroundFrame, self).__init__(parent)
        self.backgroundColor = None
        self.backgroundImage = None
        self.imageGeometry = None
        self.pixmap = None
        self.scaled_pixmap = None

    @property
    def image_position(self):
        return QPoint(0, 0) if self.imageGeometry is None else self.imageGeometry.topLeft()

    @property
    def image_size(self):
        if self.imageGeometry is not None:
            size = self.imageGeometry.size().expandedTo(QSize(0, 0)) # requested size with negative values turned to 0
            if size.isNull():
                return size if self.pixmap is None else self.pixmap.size()
            elif size.width() == 0:
                return size.expandedTo(QSize(16777215, 0))
            elif size.height() == 0:
                return size.expandedTo(QSize(0, 16777215))
            else:
                return size
        elif self.pixmap:
            return self.pixmap.size()
        else:
            return QSize(0, 0)

    def event(self, event):
        if event.type() == QEvent.DynamicPropertyChange:
            if event.propertyName() == 'backgroundImage':
                self.pixmap = QPixmap()
                if self.backgroundImage and self.pixmap.load(Resources.get(self.backgroundImage)):
                    self.scaled_pixmap = self.pixmap.scaled(self.image_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                else:
                    self.pixmap = self.scaled_pixmap = None
                self.update()
            elif event.propertyName() == 'imageGeometry' and self.pixmap:
                self.scaled_pixmap = self.pixmap.scaled(self.image_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.update()
            elif event.propertyName() == 'backgroundColor':
                self.update()
        return super(BackgroundFrame, self).event(event)

    def resizeEvent(self, event):
        self.scaled_pixmap = self.pixmap and self.pixmap.scaled(self.image_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def paintEvent(self, event):
        super(BackgroundFrame, self).paintEvent(event)
        painter = QPainter(self)
        if self.backgroundColor:
            painter.fillRect(self.rect(), QColor(self.backgroundColor))
        if self.scaled_pixmap is not None:
            painter.drawPixmap(self.image_position, self.scaled_pixmap)
        painter.end()


