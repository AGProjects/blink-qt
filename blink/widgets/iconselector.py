# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

import os

from PyQt4.QtCore import Qt
from PyQt4.QtGui import QFileDialog, QLabel, QPixmap

from blink.resources import ApplicationData, Resources
from blink.widgets.util import QtDynamicProperty


class IconSelector(QLabel):
    default_icon = QtDynamicProperty('default_icon',  unicode)

    def __init__(self, parent=None):
        super(QLabel, self).__init__(parent)
        self.setMinimumSize(36, 36)
        self.filename = None
        self.default_icon = None
        self.last_icon_directory = os.path.expanduser('~')

    def _get_filename(self):
        return self.__dict__['filename']

    def _set_filename(self, filename):
        self.__dict__['filename'] = filename
        filename = ApplicationData.get(filename) if filename else Resources.get(self.default_icon)
        pixmap = QPixmap()
        if pixmap.load(filename):
            self.setPixmap(pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.setPixmap(pixmap)

    filename = property(_get_filename, _set_filename)
    del _get_filename, _set_filename

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            filename = unicode(QFileDialog.getOpenFileName(self, u'Select Icon', self.last_icon_directory, u"Images (*.png *.tiff *.jpg *.xmp *.svg)"))
            if filename:
                self.last_icon_directory = os.path.dirname(filename)
                self.filename = filename if os.path.realpath(filename) != os.path.realpath(Resources.get(self.default_icon)) else None
        super(IconSelector, self).mouseReleaseEvent(event)


