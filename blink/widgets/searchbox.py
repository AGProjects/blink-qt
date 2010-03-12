# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

from PyQt4.QtCore import Qt, SIGNAL, SLOT
from PyQt4.QtGui import QAbstractButton, QPainter, QPalette, QPixmap, QWidget

from blink.resources import Resources
from blink.widgets.lineedit import LineEdit

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
        self.clear_button.hide()
        self.connect(self.clear_button, SIGNAL("clicked()"), self, SLOT("clear()"))
        self.connect(self, SIGNAL("textChanged(const QString&)"), self.text_changed)
        self.inactiveText = u"Search"

    def text_changed(self, text):
        self.clear_button.setVisible(not text.isEmpty())


