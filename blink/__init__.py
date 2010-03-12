# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['Blink']

from PyQt4.QtCore import Qt, SIGNAL, SLOT
from PyQt4.QtGui  import QApplication, QBrush, QColor, QPainter, QPen, QPixmap

# We need to fix __path__ in order be able to import the ui module when used
# with an interactive interpreter, because the ui module changes the current
# working directory when loading the user interfaces and this interferes with
# loading the custom classes for the user interfaces as __path__ points to a
# relative directory in that case and it won't find the submodules anymore.
import os
__path__ = [os.path.realpath(p) for p in __path__]

# We need this available early in order to import the ui module, as
# loading the user interfaces requires an instance of QApplication
import sys
_qt_application = QApplication(sys.argv)

from blink import ui
from blink.resources import Resources


class Blink(object):
    def __init__(self):
        self.app = _qt_application
        self.main_window = ui.main_window
        #self.main_window.setWindowTitle('Blink')
        self.main_window.setWindowIconText('Blink')
        self._setup_identities()
        #self._setup_user_states()

        #self.contacts_widget = uic.loadUi("contacts.ui", self.main_window.widget)
        #self.contacts_widget.hide()

        self.main_window.main_view.setCurrentWidget(self.main_window.contacts_panel)
        self.main_window.contacts_view.setCurrentWidget(self.main_window.contact_list_panel)
        self.main_window.search_view.setCurrentWidget(self.main_window.search_list_panel)

        self.main_window.connect(self.main_window.search_box, SIGNAL("textChanged(const QString&)"), self.text_changed)
        self.main_window.connect(self.main_window.back_to_contacts, SIGNAL("clicked()"), self.main_window.search_box, SLOT("clear()"))

        #self.main_window.search_box.setStyleSheet(search_css) # this method is not working properly with all themes. -Dan
        self.main_window.connect(self.main_window.identity, SIGNAL("currentIndexChanged (const QString&)"), self.set_identity)
        #self.main_window.connect(self.main_window.identity, QtCore.SIGNAL("activated(const QString&)"), self.set_identity2)

        #self.main_window.connect(self.main_window.icon_view, QtCore.SIGNAL("clicked()"), self.set_icon_view_mode)
        #self.main_window.connect(self.main_window.list_view, QtCore.SIGNAL("clicked()"), self.set_list_view_mode)
        #self.main_window.connect(self.main_window.list_view, QtCore.SIGNAL("doubleClicked(const QModelIndex &)"), self.play_game)
        #self.main_window.connect(self.main_window.list_view.selectionModel(), QtCore.SIGNAL("selectionChanged(const QItemSelection &, const QItemSelection &)"), self.selection_changed)

    def run(self):
        self.main_window.show()
        self.app.exec_()

    def _set_user_icon(self, image_file_name):
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(Qt.transparent))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QBrush(Qt.white))
        painter.setPen(QPen(painter.brush(), 0, Qt.NoPen))
        #painter.drawRoundedRect(0, 0, 32, 32, 6, 6)
        painter.drawRoundedRect(0, 0, 32, 32, 0, 0)
        icon = QPixmap()
        if icon.load(image_file_name):
            icon = icon.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.drawPixmap(0, 0, icon)
        painter.end()
        self.main_window.image.setPixmap(pixmap)

    def _setup_identities(self):
        self.main_window.identity.addItem("31208005167@ag-projects.com")
        self.main_window.identity.addItem("Bonjour")
        self._set_user_icon(Resources.get("icons/default_user_icon.png"))
        #self._set_user_icon(":/resources/icons/default_user_icon.png")

    def _setup_user_states(self):
        red_dot = QIcon()
        red_dot.addFile('icons/red-dot.svg')
        yellow_dot = QIcon()
        yellow_dot.addFile('icons/yellow-dot.svg')
        green_dot = QIcon()
        green_dot.addFile('icons/green-dot.svg')
        self.main_window.status.setIconSize(QSize(10, 10))
        self.main_window.status.addItem(green_dot, 'Available')
        self.main_window.status.addItem(yellow_dot, 'Away')
        self.main_window.status.addItem(red_dot, 'Busy')
        self.main_window.status.addItem(red_dot, 'On the phone')

    def set_identity(self, string):
        print "identity changed", string

    def set_identity2(self, string):
        print "identity (re)selected", string

    def text_changed(self, text):
        active_widget = self.main_window.contact_list_panel if text.isEmpty() else self.main_window.search_panel
        self.main_window.contacts_view.setCurrentWidget(active_widget)
        active_widget = self.main_window.search_list_panel if len(text)<3 else self.main_window.not_found_panel
        self.main_window.search_view.setCurrentWidget(active_widget)


