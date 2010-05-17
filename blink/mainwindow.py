# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['MainWindow']

from PyQt4 import uic
from PyQt4.QtCore import Qt, QVariant
from PyQt4.QtGui  import QBrush, QColor, QIcon, QPainter, QPen, QPixmap

from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.account import AccountManager, BonjourAccount

from blink.contacts import Contact, ContactGroup, ContactEditorDialog, ContactModel, ContactSearchModel
from blink.resources import Resources
from blink.util import run_in_gui_thread


ui_class, base_class = uic.loadUiType(Resources.get('blink.ui'))

class MainWindow(base_class, ui_class):
    implements(IObserver)

    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)

        with Resources.directory:
            self.setupUi(self)

        self.setWindowTitle('Blink')
        self.setWindowIconText('Blink')

        self.set_user_icon(Resources.get("icons/default-avatar.png")) # ":/resources/icons/default-avatar.png"
        self.enable_call_buttons(False)

        self.contact_model = ContactModel(self)
        self.contact_search_model = ContactSearchModel(self.contact_model, self)
        self.contact_list.setModel(self.contact_model)
        self.search_list.setModel(self.contact_search_model)

        self.contact_list.selectionModel().selectionChanged.connect(self.contact_list_selection_changed)
        self.search_box.textChanged.connect(self.contact_search_model.setFilterFixedString)

        self.contact_model.load()

        self.contact_editor = ContactEditorDialog(self.contact_model, self)

        self.contacts_panel.sibling_panel = self.sessions_panel
        self.contacts_panel.sibling_name = u'Sessions'
        self.sessions_panel.sibling_panel = self.contacts_panel
        self.sessions_panel.sibling_name = u'Contacts'

        self.main_view.setCurrentWidget(self.contacts_panel)
        self.contacts_view.setCurrentWidget(self.contact_list_panel)
        self.search_view.setCurrentWidget(self.search_list_panel)

        self.switch_view_button.clicked.connect(self.switch_main_view)

        self.search_box.textChanged.connect(self.search_box_text_changed)
        self.contact_model.itemsAdded.connect(self.contact_model_added_items)
        self.contact_model.itemsRemoved.connect(self.contact_model_removed_items)

        self.back_to_contacts_button.clicked.connect(self.search_box.clear) # this can be set in designer -Dan

        self.add_contact_button.clicked.connect(self.add_contact)
        self.add_search_contact_button.clicked.connect(self.add_contact)

        self.identity.activated[int].connect(self.set_identity)

        #self.connect(self.contact_list, QtCore.SIGNAL("doubleClicked(const QModelIndex &)"), self.double_click_action)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="SIPAccountManagerDidChangeDefaultAccount")
        notification_center.add_observer(self, name="SIPAccountManagerDidStart")
        notification_center.add_observer(self, name="SIPAccountDidActivate")
        notification_center.add_observer(self, name="SIPAccountDidDeactivate")

    def add_contact(self, clicked):
        model = self.contact_model
        selected_items = ((index.row(), model.data(index)) for index in self.contact_list.selectionModel().selectedIndexes())
        try:
            item = (item for row, item in sorted(selected_items) if type(item) in (Contact, ContactGroup)).next()
            preferred_group = item if type(item) is ContactGroup else item.group
        except StopIteration:
            try:
                preferred_group = (group for group in model.contact_groups if type(group) is ContactGroup).next()
            except StopIteration:
                preferred_group = None
        self.contact_editor.open_for_add(self.search_box.text(), preferred_group)

    def set_user_icon(self, image_file_name):
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

    def enable_call_buttons(self, enabled):
        self.audio_call_button.setEnabled(enabled)
        self.im_session_button.setEnabled(enabled)
        self.ds_session_button.setEnabled(enabled)

    def set_identity(self, index):
        account_manager = AccountManager()
        account_manager.default_account = self.identity.itemData(index).toPyObject()

    def search_box_text_changed(self, text):
        if text:
            self.main_view.setCurrentWidget(self.contacts_panel)
            self.switch_view_button.setText(u"Sessions")
            self.enable_call_buttons(True)
        else:
            selected_items = self.contact_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(len(selected_items)==1 and type(self.contact_model.data(selected_items[0])) is Contact)
            # switch to the sessions panel if there are active sessions, else to the contacts panel -Dan
        active_widget = self.contact_list_panel if text.isEmpty() else self.search_panel
        self.contacts_view.setCurrentWidget(active_widget)
        active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
        self.search_view.setCurrentWidget(active_widget)

    def contact_model_added_items(self, items):
        if self.search_box.text().isEmpty():
            return
        active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
        self.search_view.setCurrentWidget(active_widget)

    def contact_model_removed_items(self, items):
        if self.search_box.text().isEmpty():
            return
        if any(type(item) is Contact for item in items) and self.contact_search_model.rowCount() == 0:
            self.search_box.clear()
        else:
            active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
            self.search_view.setCurrentWidget(active_widget)

    def contact_list_selection_changed(self, selected, deselected):
        selected_items = self.contact_list.selectionModel().selectedIndexes()
        self.enable_call_buttons(len(selected_items)==1 and type(self.contact_model.data(selected_items[0])) is Contact)

    def switch_main_view(self):
        widget = self.main_view.currentWidget().sibling_panel
        self.main_view.setCurrentWidget(widget)
        self.switch_view_button.setText(widget.sibling_name)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountDidActivate(self, notification):
        account = notification.sender
        name = u'Bonjour' if account is BonjourAccount() else account.id
        icon = None
        if account is BonjourAccount():
            pixmap = QPixmap()
            if pixmap.load(Resources.get('icons/bonjour.png')):
                pixmap = pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                icon = QIcon(pixmap)
        if icon is not None:
            self.identity.addItem(icon, name, QVariant(account))
        else:
            self.identity.addItem(name, QVariant(account))

    def _NH_SIPAccountDidDeactivate(self, notification):
        account = notification.sender
        name = u'Bonjour' if account is BonjourAccount() else account.id
        self.identity.removeItem(self.identity.findText(name))

    def _NH_SIPAccountManagerDidStart(self, notification):
        account = AccountManager().default_account
        name = u'Bonjour' if account is BonjourAccount() else account.id
        self.identity.setCurrentIndex(self.identity.findText(name))

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        account = notification.data.account
        name = u'Bonjour' if account is BonjourAccount() else account.id
        self.identity.setCurrentIndex(self.identity.findText(name))

del ui_class, base_class


