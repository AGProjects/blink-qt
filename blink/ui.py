# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['main_window', 'ContactWidget', 'ContactGroupWidget']

import os

from PyQt4 import uic
from PyQt4.QtCore import Qt, QEvent, QPointF
from PyQt4.QtGui  import QBrush, QColor, QLinearGradient, QKeyEvent, QMouseEvent, QPainter, QPen, QPolygonF

from blink.resources import Resources


original_directory = os.getcwd()
os.chdir(Resources.directory)

main_window = uic.loadUi(Resources.get('blink.ui'))

ui_class, base_class = uic.loadUiType(Resources.get('contact.ui'))

class ContactWidget(base_class, ui_class):
    def __init__(self, parent=None):
        super(ContactWidget, self).__init__(parent)
        self.setupUi(self)

    def set_contact(self, contact):
        self.name.setText(contact.name)
        self.uri.setText(contact.uri)
        self.icon.setPixmap(contact.icon)

del ui_class, base_class

ui_class, base_class = uic.loadUiType(Resources.get('contact_group.ui'))

class ContactGroupWidget(base_class, ui_class):
    def __init__(self, name, parent=None):
        super(ContactGroupWidget, self).__init__(parent)
        self.setupUi(self)
        self.name = name
        self.selected = False
        self.setFocusProxy(parent)
        self.label_widget.setFocusProxy(self)
        self.name_view.setCurrentWidget(self.label_widget)
        self.name_editor.editingFinished.connect(self._end_editing)

    @property
    def collapsed(self):
        return self.arrow.isChecked()

    @property
    def editing(self):
        return self.name_view.currentWidget() is self.editor_widget

    def _get_name(self):
        return self.name_label.text()

    def _set_name(self, value):
        self.name_label.setText(value)
        self.name_editor.setText(value)

    name = property(_get_name, _set_name)
    del _get_name, _set_name

    def _get_selected(self):
        return self.__dict__['selected']

    def _set_selected(self, value):
        if self.__dict__.get('selected', None) == value:
            return
        self.__dict__['selected'] = value
        self.name_label.setStyleSheet("color: #ffffff; font-weight: bold;" if value else "color: #000000;")
        #self.name_label.setForegroundRole(QPalette.BrightText if value else QPalette.WindowText)
        self.update()

    selected = property(_get_selected, _set_selected)
    del _get_selected, _set_selected

    def _start_editing(self):
        #self.name_editor.setText(self.name_label.text())
        self.name_editor.selectAll()
        self.name_view.setCurrentWidget(self.editor_widget)
        self.name_editor.setFocus()

    def _end_editing(self):
        self.name_label.setText(self.name_editor.text())
        self.name_view.setCurrentWidget(self.label_widget)

    def paintEvent(self, event):
        painter = QPainter(self)

        background = QLinearGradient(0, 0, self.width(), self.height())
        if self.selected:
            background.setColorAt(0.0, QColor('#dadada'))
            background.setColorAt(1.0, QColor('#c4c4c4'))
            foreground = QColor('#ffffff')
        else:
            background.setColorAt(0.0, QColor('#eeeeee'))
            background.setColorAt(1.0, QColor('#d8d8d8'))
            foreground = QColor('#888888')

        rect = self.rect()

        painter.fillRect(rect, QBrush(background))

        painter.setPen(QColor('#f8f8f8'))
        painter.drawLine(rect.topLeft(), rect.topRight())
        #painter.drawLine(option.rect.topLeft(), option.rect.bottomLeft())

        painter.setPen(QColor('#b8b8b8'))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        #painter.drawLine(option.rect.topRight(), option.rect.bottomRight())

        if self.collapsed:
            arrow = QPolygonF([QPointF(0, 0), QPointF(0, 9), QPointF(8, 4.5)])
            arrow.translate(QPointF(5, 4))
        else:
            arrow = QPolygonF([QPointF(0, 0), QPointF(9, 0), QPointF(4.5, 8)])
            arrow.translate(QPointF(5, 5))
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(foreground)
        painter.setPen(QPen(painter.brush(), 0, Qt.NoPen))
        painter.drawPolygon(arrow)
        painter.end()

    def event(self, event):
        if type(event) is QKeyEvent and self.editing:
            return True # do not propagate keyboard events while editing
        elif type(event) is QMouseEvent and event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
            self._start_editing()
        return super(ContactGroupWidget, self).event(event)

del ui_class, base_class

os.chdir(original_directory)
del original_directory

