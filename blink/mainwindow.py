# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['MainWindow']

from PyQt4 import uic
from PyQt4.QtCore import Qt
from PyQt4.QtGui  import QBrush, QColor, QPainter, QPen, QPixmap

from blink.contacts import ContactDelegate, ContactModel, ContactSearchModel
from blink.resources import Resources


ui_class, base_class = uic.loadUiType(Resources.get('blink.ui'))

class MainWindow(base_class, ui_class):
    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)

        with Resources.directory:
            self.setupUi(self)

        self.setWindowTitle('Blink')
        self.setWindowIconText('Blink')

        self._setup_identities()

        self.contact_model = ContactModel(self)
        self.contact_list.setModel(self.contact_model)
        self.contact_list.setItemDelegate(ContactDelegate(self.contact_list))
        self.contact_model.test()

        self.contact_search_model = ContactSearchModel(self.contact_model, self)
        self.search_list.setModel(self.contact_search_model)
        self.search_list.setItemDelegate(ContactDelegate(self.search_list))
        self.search_box.textChanged.connect(self.contact_search_model.setFilterFixedString)

        self.contacts_panel.sibling_panel = self.sessions_panel
        self.contacts_panel.sibling_name = u'Sessions'
        self.sessions_panel.sibling_panel = self.contacts_panel
        self.sessions_panel.sibling_name = u'Contacts'

        self.main_view.setCurrentWidget(self.contacts_panel)
        self.contacts_view.setCurrentWidget(self.contact_list_panel)
        self.search_view.setCurrentWidget(self.search_list_panel)

        self.switch_view.clicked.connect(self.switch_main_view)

        self.search_box.textChanged.connect(self.search_box_text_changed)

        self.back_to_contacts.clicked.connect(self.search_box.clear) # this can be set in designer -Dan
        self.add_contact.clicked.connect(self.test_add_contact)

        self.identity.currentIndexChanged[str].connect(self.set_identity)

        #self.connect(self.contact_list, QtCore.SIGNAL("doubleClicked(const QModelIndex &)"), self.double_click_action)
        #self.connect(self.contact_list.selectionModel(), QtCore.SIGNAL("selectionChanged(const QItemSelection &, const QItemSelection &)"), self.selection_changed)

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
        self.image.setPixmap(pixmap)

    def _setup_identities(self):
        self.identity.addItem("31208005167@ag-projects.com")
        self.identity.addItem("Bonjour")
        self._set_user_icon(Resources.get("icons/default-avatar.png"))
        #self._set_user_icon(":/resources/icons/default-avatar.png")

    def set_identity(self, string):
        print "identity changed", string

    def search_box_text_changed(self, text):
        if text:
            self.main_view.setCurrentWidget(self.contacts_panel)
            self.switch_view.setText(u"Sessions")
        else:
            # switch to the sessions panel if there are active sessions, else to the contacts panel -Dan
            pass
        active_widget = self.contact_list_panel if text.isEmpty() else self.search_panel
        self.contacts_view.setCurrentWidget(active_widget)
        active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
        self.search_view.setCurrentWidget(active_widget)

    def switch_main_view(self):
        widget = self.main_view.currentWidget().sibling_panel
        self.main_view.setCurrentWidget(widget)
        self.switch_view.setText(widget.sibling_name)

    def test_add_contact(self):
        from blink.contacts import Contact, ContactGroup
        import random
        no = random.randrange(1, 100)
        contact = Contact(ContactGroup('Test'), 'John Doe %02d' % no, 'user%02d@test.com' % no)
        contact.status = random.choice(('online', 'away', 'busy', 'offline'))
        self.contact_model.addContact(contact)

del ui_class, base_class


