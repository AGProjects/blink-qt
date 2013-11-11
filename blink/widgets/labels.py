# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['DurationLabel', 'IconSelector', 'LatencyLabel', 'PacketLossLabel', 'Status', 'StatusLabel', 'StreamInfoLabel', 'ElidedLabel', 'ContactState']

import os
from datetime import timedelta

from PyQt4.QtCore import Qt, QEvent
from PyQt4.QtGui import QAction, QBrush, QColor, QFileDialog, QFontMetrics, QIcon, QLabel, QLinearGradient, QMenu, QPainter, QPalette, QPen

from application.python.types import MarkerType

from blink.resources import IconManager
from blink.widgets.color import ColorHelperMixin
from blink.widgets.util import QtDynamicProperty


class ContextMenuActions(object):
    pass


class IconSelector(QLabel):
    default_icon = QtDynamicProperty('default_icon', QIcon)
    icon_size = QtDynamicProperty('icon_size', int)

    class NotSelected: __metaclass__ = MarkerType

    def __init__(self, parent=None):
        super(IconSelector, self).__init__(parent)
        self.actions = ContextMenuActions()
        self.actions.select_icon = QAction(u'Select icon...', self, triggered=self._SH_ChangeIconActionTriggered)
        self.actions.remove_icon = QAction(u'Use contact provided icon', self, triggered=self._SH_RemoveIconActionTriggered)
        self.icon_size = 48
        self.default_icon = None
        self.contact_icon = None
        self.icon = None
        self.filename = self.NotSelected
        self.last_icon_directory = os.path.expanduser('~')

    def _get_icon(self):
        return self.__dict__['icon']

    def _set_icon(self, icon):
        self.__dict__['icon'] = icon
        icon = icon or self.default_icon or QIcon()
        self.setPixmap(icon.pixmap(self.icon_size))

    icon = property(_get_icon, _set_icon)
    del _get_icon, _set_icon

    def _get_filename(self):
        return self.__dict__['filename']

    def _set_filename(self, filename):
        self.__dict__['filename'] = filename
        if filename is self.NotSelected:
            return
        elif filename is None:
            self.icon = self.contact_icon
        else:
            self.icon = QIcon(filename)
            self.last_icon_directory = os.path.dirname(filename)

    filename = property(_get_filename, _set_filename)
    del _get_filename, _set_filename

    def init_with_contact(self, contact):
        if contact is None:
            self.icon = self.contact_icon = None
        else:
            icon_manager = IconManager()
            self.contact_icon = icon_manager.get(contact.id)
            self.icon = icon_manager.get(contact.id + '_alt') or self.contact_icon
            if contact.alternate_icon is not None:
                self.last_icon_directory = os.path.dirname(contact.alternate_icon.url.path)
        self.filename = self.NotSelected

    def update_from_contact(self, contact):
        icon_manager = IconManager()
        if self.icon is self.contact_icon:
            self.icon = self.contact_icon = icon_manager.get(contact.id)
        else:
            self.contact_icon = icon_manager.get(contact.id)

    def event(self, event):
        if event.type() == QEvent.DynamicPropertyChange and event.propertyName() == 'icon_size':
            self.setFixedSize(self.icon_size+12, self.icon_size+12)
            self.update()
        return super(IconSelector, self).event(event)

    def enterEvent(self, event):
        icon = self.icon or self.default_icon or QIcon()
        self.setPixmap(icon.pixmap(self.icon_size, mode=QIcon.Selected))
        super(IconSelector, self).enterEvent(event)

    def leaveEvent(self, event):
        icon = self.icon or self.default_icon or QIcon()
        self.setPixmap(icon.pixmap(self.icon_size, mode=QIcon.Normal))
        super(IconSelector, self).leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.actions.remove_icon.setEnabled(self.icon is not self.contact_icon)
            menu = QMenu(self)
            menu.addAction(self.actions.select_icon)
            menu.addAction(self.actions.remove_icon)
            menu.exec_(self.mapToGlobal(self.rect().translated(0, 2).bottomLeft()))
        super(IconSelector, self).mouseReleaseEvent(event)

    def _SH_ChangeIconActionTriggered(self):
        filename = QFileDialog.getOpenFileName(self, u'Select Icon', self.last_icon_directory, u"Images (*.png *.tiff *.jpg *.xmp *.svg)")
        if filename:
            self.filename = filename

    def _SH_RemoveIconActionTriggered(self):
        self.filename = None


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


class DurationLabel(QLabel):
    def __init__(self, parent=None):
        super(DurationLabel, self).__init__(parent)
        self.value = timedelta(0)

    def _get_value(self):
        return self.__dict__['value']

    def _set_value(self, value):
        self.__dict__['value'] = value
        seconds = value.seconds % 60
        minutes = value.seconds // 60 % 60
        hours = value.seconds//3600 + value.days*24
        self.setText(u'%d:%02d:%02d' % (hours, minutes, seconds))

    value = property(_get_value, _set_value)
    del _get_value, _set_value


class LatencyLabel(QLabel):
    def __init__(self, parent=None):
        super(LatencyLabel, self).__init__(parent)
        self.threshold = 99
        self.value = 0

    def _get_value(self):
        return self.__dict__['value']

    def _set_value(self, value):
        self.__dict__['value'] = value
        if value > self.threshold:
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
        self.threshold = 3
        self.value = 0

    def _get_value(self):
        return self.__dict__['value']

    def _set_value(self, value):
        self.__dict__['value'] = value
        if value > self.threshold:
            text = u'Packet loss %s%%' % value
            self.setMinimumWidth(QFontMetrics(self.font()).width(text))
            self.setText(text)
            self.show()
        else:
            self.hide()

    value = property(_get_value, _set_value)
    del _get_value, _set_value


class Status(unicode):
    def __new__(cls, value, color='black'):
        instance = super(Status, cls).__new__(cls, value)
        instance.color = color
        return instance

    def __eq__(self, other):
        if isinstance(other, Status):
            return super(Status, self).__eq__(other) and self.color == other.color
        elif isinstance(other, basestring):
            return super(Status, self).__eq__(other)
        return NotImplemented

    def __ne__(self, other):
        return not (self == other)


class StatusLabel(QLabel):
    def __init__(self, parent=None):
        super(StatusLabel, self).__init__(parent)
        self.value = None

    def _get_value(self):
        return self.__dict__['value']

    def _set_value(self, value):
        self.__dict__['value'] = value
        if value is not None:
            color = QColor(value.color)
            palette = self.palette()
            palette.setColor(QPalette.WindowText, color)
            palette.setColor(QPalette.Text, color)
            self.setPalette(palette)
            self.setText(unicode(value))
        else:
            self.setText(u'')

    value = property(_get_value, _set_value)
    del _get_value, _set_value


class ElidedLabel(QLabel):
    """A label that elides the text using a fading gradient"""

    def paintEvent(self, event):
        painter = QPainter(self)
        font_metrics = QFontMetrics(self.font())
        if font_metrics.width(self.text()) > self.contentsRect().width():
            label_width = self.size().width()
            gradient = QLinearGradient(0, 0, label_width, 0)
            gradient.setColorAt(1-50.0/label_width, self.palette().color(self.foregroundRole()))
            gradient.setColorAt(1.0, Qt.transparent)
            painter.setPen(QPen(QBrush(gradient), 1.0))
        painter.drawText(self.rect(), Qt.TextSingleLine | int(self.alignment()), self.text())


class StateColor(QColor):
    @property
    def stroke(self):
        return self.darker(200)

class StateColorMapping(dict):
    def __missing__(self, key):
        if key == 'offline':
            return self.setdefault(key, StateColor('#d0d0d0'))
        elif key == 'available':
            return self.setdefault(key, StateColor('#00ff00'))
        elif key == 'away':
            return self.setdefault(key, StateColor('#ffff00'))
        elif key == 'busy':
            return self.setdefault(key, StateColor('#ff0000'))
        else:
            return StateColor(Qt.transparent) #StateColor('#d0d0d0')


class ContactState(QLabel, ColorHelperMixin):
    state = QtDynamicProperty('state', unicode)

    def __init__(self, parent=None):
        super(ContactState, self).__init__(parent)
        self.state_colors = StateColorMapping()
        self.state = None

    def event(self, event):
        if event.type() == QEvent.DynamicPropertyChange and event.propertyName() == 'state':
            self.update()
        return super(ContactState, self).event(event)

    def paintEvent(self, event):
        color = self.state_colors[self.state]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0.0, Qt.transparent)
        gradient.setColorAt(1.0, color)
        painter.setBrush(QBrush(gradient))
        gradient.setColorAt(1.0, color.stroke)
        painter.setPen(QPen(QBrush(gradient), 1))
        painter.drawRoundedRect(-4, 0, self.width()+4, self.height(), 3.7, 3.7)


