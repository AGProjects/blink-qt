# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['LineEdit', 'ValidatingLineEdit', 'SearchBox', 'LocationBar']

import re

from PyQt4.QtCore import Qt, QEvent, pyqtSignal
from PyQt4.QtGui import QAbstractButton, QLineEdit, QBoxLayout, QHBoxLayout, QLabel, QLayout, QPainter, QPalette, QPixmap, QSpacerItem, QSizePolicy, QStyle, QWidget, QStyleOptionFrameV2

from blink.resources import Resources
from blink.widgets.util import QtDynamicProperty


class SideWidget(QWidget):
    sizeHintChanged = pyqtSignal()

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)

    def event(self, event):
        if event.type() == QEvent.LayoutRequest:
            self.sizeHintChanged.emit()
        return QWidget.event(self, event)


class LineEdit(QLineEdit):
    inactiveText  = QtDynamicProperty('inactiveText',  unicode)
    widgetSpacing = QtDynamicProperty('widgetSpacing', int)

    def __init__(self, parent=None, contents=u""):
        QLineEdit.__init__(self, contents, parent)
        box_direction = QBoxLayout.RightToLeft if self.isRightToLeft() else QBoxLayout.LeftToRight
        self.inactiveText = u""
        self.left_widget = SideWidget(self)
        self.left_widget.resize(0, 0)
        self.left_layout = QHBoxLayout(self.left_widget)
        self.left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_layout.setDirection(box_direction)
        self.left_layout.setSizeConstraint(QLayout.SetFixedSize)
        self.right_widget = SideWidget(self)
        self.right_widget.resize(0, 0)
        self.right_layout = QHBoxLayout(self.right_widget)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setDirection(box_direction)
        self.right_layout.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum))
        self.widgetSpacing = 2
        self.left_widget.sizeHintChanged.connect(self._update_text_margins)
        self.right_widget.sizeHintChanged.connect(self._update_text_margins)

    @property
    def left_margin(self):
        return self.left_widget.sizeHint().width() + 2*self.right_layout.spacing()

    @property
    def right_margin(self):
        return self.right_widget.sizeHint().width() + 2*self.right_layout.spacing()

    def _update_text_margins(self):
        self.setTextMargins(self.left_margin, 0, self.right_margin, 0)
        self._update_side_widget_locations()

    def _update_side_widget_locations(self):
        option = QStyleOptionFrameV2()
        self.initStyleOption(option)
        spacing = self.right_layout.spacing()
        text_rect = self.style().subElementRect(QStyle.SE_LineEditContents, option, self)
        text_rect.adjust(spacing, 0, -spacing, 0)
        mid_height = text_rect.center().y() + 1 - (text_rect.height() % 2) # need -1 correction for odd heights -Dan
        if self.left_layout.count() > 0:
            left_height = mid_height - self.left_widget.height()/2
            left_width = self.left_widget.width()
            if left_width == 0:
                left_height = mid_height - self.left_widget.sizeHint().height()/2
            self.left_widget.move(text_rect.x(), left_height)
        text_rect.setX(self.left_margin)
        text_rect.setY(mid_height - self.right_widget.sizeHint().height()/2.0)
        text_rect.setHeight(self.right_widget.sizeHint().height())
        self.right_widget.setGeometry(text_rect)

    def event(self, event):
        if event.type() == QEvent.LayoutDirectionChange:
            box_direction = QBoxLayout.RightToLeft if self.isRightToLeft() else QBoxLayout.LeftToRight
            self.left_layout.setDirection(box_direction)
            self.right_layout.setDirection(box_direction)
        elif event.type() == QEvent.DynamicPropertyChange and event.propertyName() == 'widgetSpacing':
            self.left_layout.setSpacing(self.widgetSpacing)
            self.right_layout.setSpacing(self.widgetSpacing)
            self._update_text_margins()
        return QLineEdit.event(self, event)

    def resizeEvent(self, event):
        self._update_side_widget_locations()
        QLineEdit.resizeEvent(self, event)

    def paintEvent(self, event):
        QLineEdit.paintEvent(self, event)
        if not self.hasFocus() and self.text().isEmpty() and self.inactiveText:
            options = QStyleOptionFrameV2()
            self.initStyleOption(options)
            text_rect = self.style().subElementRect(QStyle.SE_LineEditContents, options, self)
            text_rect.adjust(self.left_margin+2, 0, -self.right_margin, 0)
            painter = QPainter(self)
            painter.setPen(self.palette().brush(QPalette.Disabled, QPalette.Text).color())
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, self.inactiveText)

    def addHeadWidget(self, widget):
        if self.isRightToLeft():
            self.right_layout.insertWidget(1, widget)
        else:
            self.left_layout.addWidget(widget)

    def addTailWidget(self, widget):
        if self.isRightToLeft():
            self.left_layout.addWidget(widget)
        else:
            self.right_layout.insertWidget(1, widget)

    def removeWidget(self, widget):
        self.left_layout.removeWidget(widget)
        self.right_layout.removeWidget(widget)
        widget.hide()


class ValidatingLineEdit(LineEdit):
    statusChanged = pyqtSignal()

    def __init__(self, parent=None):
        super(ValidatingLineEdit, self).__init__(parent)
        self.invalid_entry_label = QLabel(self)
        self.invalid_entry_label.setFixedSize(18, 16)
        self.invalid_entry_label.setPixmap(QPixmap(Resources.get('icons/invalid16.png')))
        self.invalid_entry_label.setScaledContents(False)
        self.invalid_entry_label.setAlignment(Qt.AlignCenter)
        self.invalid_entry_label.setObjectName('invalid_entry_label')
        self.invalid_entry_label.hide()
        self.addTailWidget(self.invalid_entry_label)
        option = QStyleOptionFrameV2()
        self.initStyleOption(option)
        frame_width = self.style().pixelMetric(QStyle.PM_DefaultFrameWidth, option, self)
        self.setMinimumHeight(self.invalid_entry_label.minimumHeight() + 2 + 2*frame_width)
        self.textChanged.connect(self._SH_TextChanged)
        self.text_correct = True
        self.text_allowed = True
        self.exceptions = set()
        self.regexp = re.compile(r'.*')

    def _get_regexp(self):
        return self.__dict__['regexp']

    def _set_regexp(self, regexp):
        self.__dict__['regexp'] = regexp
        self._validate()

    regexp = property(_get_regexp, _set_regexp)
    del _get_regexp, _set_regexp

    @property
    def text_valid(self):
        return self.text_correct and self.text_allowed

    def _SH_TextChanged(self, text):
        self._validate()

    def _validate(self):
        text = unicode(self.text())
        text_correct = self.regexp.search(text) is not None
        text_allowed = text not in self.exceptions
        if self.text_correct != text_correct or self.text_allowed != text_allowed:
            self.text_correct = text_correct
            self.text_allowed = text_allowed
            self.invalid_entry_label.setVisible(not self.text_valid)
            self.statusChanged.emit()

    def addException(self, exception):
        self.exceptions.add(exception)
        self._validate()

    def removeException(self, exception):
        self.exceptions.remove(exception)
        self._validate()


class SearchIcon(QWidget):
    def __init__(self, parent=None, size=16):
        QWidget.__init__(self, parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVisible(True)
        self.setMinimumSize(size+2, size+2)
        pixmap = QPixmap()
        if pixmap.load(Resources.get("icons/search.svg")):
            self.icon = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            self.icon = None

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.icon is not None:
            x = (self.width() - self.icon.width()) / 2
            y = (self.height() - self.icon.height()) / 2
            painter.drawPixmap(x, y, self.icon)


class ClearButton(QAbstractButton):
    def __init__(self, parent=None, size=16):
        QAbstractButton.__init__(self, parent)
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(u"Clear")
        self.setVisible(False)
        self.setMinimumSize(size+2, size+2)
        pixmap = QPixmap()
        if pixmap.load(Resources.get("icons/delete.svg")):
            self.icon = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            # Use QImage because QPainter using a QPixmap does not support CompositionMode_Multiply -Dan
            image = self.icon.toImage()
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setCompositionMode(QPainter.CompositionMode_Multiply)
            painter.drawPixmap(0, 0, self.icon)
            painter.end()
            self.icon_pressed = QPixmap(image)
        else:
            self.icon = self.icon_pressed = None

    def paintEvent(self, event):
        painter = QPainter(self)
        icon = self.icon_pressed if self.isDown() else self.icon
        if icon is not None:
            x = (self.width() - icon.width()) / 2
            y = (self.height() - icon.height()) / 2
            painter.drawPixmap(x, y, icon)
        else:
            width = self.width()
            height = self.height()

            padding = width / 5
            radius = width - 2*padding

            palette = self.palette()

            # Mid is darker than Dark. Go figure... -Dan
            bg_color = palette.color(QPalette.Mid) if self.isDown() else palette.color(QPalette.Dark)
            fg_color = palette.color(QPalette.Window) # or QPalette.Base for white

            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(bg_color)
            painter.setPen(bg_color)
            painter.drawEllipse(padding, padding, radius, radius)

            padding = padding * 2
            painter.setPen(fg_color)
            painter.drawLine(padding, padding, width-padding, height-padding)
            painter.drawLine(padding, height-padding, width-padding, padding)


class SearchBox(LineEdit):
    def __init__(self, parent=None):
        LineEdit.__init__(self, parent=parent)
        self.search_icon = SearchIcon(self)
        self.clear_button = ClearButton(self)
        self.addHeadWidget(self.search_icon)
        self.addTailWidget(self.clear_button)
        option = QStyleOptionFrameV2()
        self.initStyleOption(option)
        frame_width = self.style().pixelMetric(QStyle.PM_DefaultFrameWidth, option, self)
        widgets_height = max(self.search_icon.minimumHeight(), self.clear_button.minimumHeight())
        self.setMinimumHeight(widgets_height + 2 + 2*frame_width)
        self.clear_button.hide()
        self.clear_button.clicked.connect(self.clear)
        self.textChanged.connect(self._SH_TextChanged)
        self.inactiveText = u"Search"

    def _SH_TextChanged(self, text):
        self.clear_button.setVisible(not text.isEmpty())


class LocationBar(LineEdit):
    locationCleared = pyqtSignal()

    def __init__(self, parent=None):
        LineEdit.__init__(self, parent=parent)
        self.clear_button = ClearButton(self)
        self.addTailWidget(self.clear_button)
        option = QStyleOptionFrameV2()
        self.initStyleOption(option)
        frame_width = self.style().pixelMetric(QStyle.PM_DefaultFrameWidth, option, self)
        widgets_height = self.clear_button.minimumHeight()
        self.setMinimumHeight(widgets_height + 2 + 2*frame_width)
        self.clear_button.hide()
        self.clear_button.clicked.connect(self._SH_ClearButtonClicked)
        self.textChanged.connect(self._SH_TextChanged)

    def _SH_ClearButtonClicked(self):
        self.clear()
        self.locationCleared.emit()

    def _SH_TextChanged(self, text):
        self.clear_button.setVisible(not text.isEmpty())


