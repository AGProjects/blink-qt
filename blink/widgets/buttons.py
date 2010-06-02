# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['ToolButton', 'SegmentButton', 'SingleSegment', 'LeftSegment', 'MiddleSegment', 'RightSegment']

from PyQt4.QtCore import pyqtSignal
from PyQt4.QtGui  import QStyle, QStyleOptionToolButton, QStylePainter, QToolButton


class ToolButton(QToolButton):
    """A custom QToolButton that doesn't show a menu indicator arrow"""
    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.features &= ~QStyleOptionToolButton.HasMenu
        painter.drawComplexControl(QStyle.CC_ToolButton, option)


class SegmentTypeMeta(type):
    def __repr__(cls):
        return cls.__name__

class SegmentType(object):
    __metaclass__ = SegmentTypeMeta
    style_sheet = ''

class SingleSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-radius: 4px;
                         border-width: 1px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                         border-style: inset;
                     }
                  """

class LeftSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-top-left-radius: 4px;
                         border-bottom-left-radius: 4px;
                         border-width: 1px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                         border-style: inset;
                     }
                  """

class MiddleSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-width: 1px;
                         border-left-width: 0px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                         border-style: inset;
                     }
                  """

class RightSegment(SegmentType):
    style_sheet = """
                     QToolButton {
                         background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fafafa, stop:1 #bababa);
                         border-color: #545454;
                         border-top-right-radius: 4px;
                         border-bottom-right-radius: 4px;
                         border-width: 1px;
                         border-left-width: 0px;
                         border-style: solid;
                     }
                     
                     QToolButton:pressed {
                         background: qradialgradient(cx:0.5, cy:0.5, radius:1, fx:0.5, fy:0.5, stop:0 #dddddd, stop:1 #777777);
                         border-style: inset;
                     }
                  """


class SegmentButton(QToolButton):
    SingleSegment = SingleSegment
    LeftSegment   = LeftSegment
    MiddleSegment = MiddleSegment
    RightSegment  = RightSegment

    hidden = pyqtSignal()
    shown  = pyqtSignal()

    def __init__(self, parent=None):
        super(SegmentButton, self).__init__(parent)
        self.type = SingleSegment

    def _get_type(self):
        return self.__dict__['type']

    def _set_type(self, value):
        if not issubclass(value, SegmentType):
            raise ValueError("Invalid type: %r" % value)
        self.__dict__['type'] = value
        self.setStyleSheet(value.style_sheet)

    type = property(_get_type, _set_type)
    del _get_type, _set_type

    def hide(self):
        super(SegmentButton, self).hide()
        self.hidden.emit()

    def show(self):
        super(SegmentButton, self).show()
        self.shown.emit()


