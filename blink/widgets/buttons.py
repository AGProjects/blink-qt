# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['ToolButton']

from PyQt4.QtGui import QStyle, QStyleOptionToolButton, QStylePainter, QToolButton


class ToolButton(QToolButton):
    """A custom QToolButton that doesn't show a menu indicator arrow"""
    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.features &= ~QStyleOptionToolButton.HasMenu
        painter.drawComplexControl(QStyle.CC_ToolButton, option)


