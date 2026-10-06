from PyQt6.QtCore import QSize
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import QApplication


__all__ = ['QtDynamicProperty', 'ContextMenuActions', 'FontScaledSize', 'badge_font']


class QtDynamicProperty(object):
    def __init__(self, name, type=str):
        self.name = name
        self.type = type

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.property(self.name)

    def __set__(self, obj, value):
        if value is not None and not isinstance(value, self.type):
            value = self.type(value)
        obj.setProperty(self.name, value)

    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class ContextMenuActions(object):
    pass


class FontScaledSize(object):
    """A list row's size hint that grows with the application font: `lines` lines of text plus
    `padding`, never below the height the row was designed at."""

    def __init__(self, width, minimum_height, lines, padding):
        self.width = width
        self.minimum_height = minimum_height
        self.lines = lines
        self.padding = padding
        self._cache = (None, None)

    def __get__(self, instance, owner):
        font = QApplication.font()
        key = font.key()
        if self._cache[0] != key:
            line_height = QFontMetrics(font).height()
            self._cache = key, QSize(self.width, max(self.minimum_height, self.lines * line_height + self.padding))
        return self._cache[1]


def badge_font(font):
    """The unread badge's font: bold, a point smaller than the text around it."""
    font = QFont(font)
    if font.pointSizeF() > 0:
        font.setPointSizeF(max(font.pointSizeF() - 1, 6))
    font.setBold(True)
    return font
