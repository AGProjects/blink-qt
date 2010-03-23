# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

from PyQt4.QtCore import Qt, QEvent, pyqtSignal
from PyQt4.QtGui import QLineEdit, QBoxLayout, QHBoxLayout, QLayout, QPainter, QPalette, QSpacerItem, QSizePolicy, QStyle, QWidget, QStyleOptionFrameV2

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
    inactiveText  = QtDynamicProperty('inactiveText',  str)
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


