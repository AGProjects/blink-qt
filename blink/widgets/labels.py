# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['IconSelector', 'LatencyLabel', 'PacketLossLabel', 'StreamInfoLabel']

import os

from PyQt4.QtCore import Qt
from PyQt4.QtGui import QFileDialog, QFontMetrics, QLabel, QPixmap

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


class StreamInfoLabel(QLabel):
    def __init__(self, parent=None):
        super(StreamInfoLabel, self).__init__(parent)
        self.session_type = None
        self.codec_info = ''

    def _get_session_type(self):
        return self.__dict__['session_type']

    def _set_session_type(self, value):
        self.__dict__['session_type'] = value
        self.update_content()

    session_type = property(_get_session_type, _set_session_type)
    del _get_session_type, _set_session_type

    def _get_codec_info(self):
        return self.__dict__['codec_info']

    def _set_codec_info(self, value):
        self.__dict__['codec_info'] = value
        self.update_content()

    codec_info = property(_get_codec_info, _set_codec_info)
    del _get_codec_info, _set_codec_info

    def resizeEvent(self, event):
        super(StreamInfoLabel, self).resizeEvent(event)
        self.update_content()

    def update_content(self):
        if self.session_type and self.codec_info:
            text = u'%s (%s)' % (self.session_type, self.codec_info)
            if self.width() < QFontMetrics(self.font()).width(text):
                text = self.session_type
        else:
            text = self.session_type or u''
        self.setText(text)


class LatencyLabel(QLabel):
    def __init__(self, parent=None):
        super(LatencyLabel, self).__init__(parent)
        self.treshold = 99
        self.value = 0

    def _get_value(self):
        return self.__dict__['value']

    def _set_value(self, value):
        self.__dict__['value'] = value
        if value > self.treshold:
            text = u'Latency %sms' % value
            self.setMinimumWidth(QFontMetrics(self.font()).width(text))
            self.setText(text)
            self.show()
        else:
            self.hide()

    value = property(_get_value, _set_value)
    del _get_value, _set_value


class PacketLossLabel(QLabel):
    def __init__(self, parent=None):
        super(PacketLossLabel, self).__init__(parent)
        self.treshold = 0
        self.value = 0

    def _get_value(self):
        return self.__dict__['value']

    def _set_value(self, value):
        self.__dict__['value'] = value
        if value > self.treshold:
            text = u'Packet loss %s%%' % value
            self.setMinimumWidth(QFontMetrics(self.font()).width(text))
            self.setText(text)
            self.show()
        else:
            self.hide()

    value = property(_get_value, _set_value)
    del _get_value, _set_value


