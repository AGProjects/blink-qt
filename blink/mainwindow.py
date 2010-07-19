# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['MainWindow']

from functools import partial

from PyQt4 import uic
from PyQt4.QtCore import Qt, QVariant
from PyQt4.QtGui  import QAction, QActionGroup, QBrush, QColor, QFontMetrics, QPainter, QPen, QPixmap, QShortcut, QStyle, QStyleOptionComboBox, QStyleOptionFrameV2

from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings

from blink.aboutpanel import AboutPanel
from blink.accounts import AccountModel, ActiveAccountModel, AddAccountDialog
from blink.contacts import BonjourNeighbour, Contact, ContactGroup, ContactEditorDialog, ContactModel, ContactSearchModel
from blink.sessions import SessionManager, SessionModel
from blink.resources import Resources
from blink.util import call_in_auxiliary_thread, run_in_gui_thread
from blink.widgets.buttons import SwitchViewButton


ui_class, base_class = uic.loadUiType(Resources.get('blink.ui'))

class MainWindow(base_class, ui_class):
    implements(IObserver)

    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)

        with Resources.directory:
            self.setupUi()

        self.setWindowTitle('Blink')
        self.setWindowIconText('Blink')

        self.set_user_icon(Resources.get("icons/default-avatar.png")) # ":/resources/icons/default-avatar.png"
        self.enable_call_buttons(False)
        self.active_sessions_label.hide()

        self.account_model = AccountModel(self)
        self.enabled_account_model = ActiveAccountModel(self.account_model, self)
        self.identity.setModel(self.enabled_account_model)

        self.contact_model = ContactModel(self)
        self.contact_search_model = ContactSearchModel(self.contact_model, self)
        self.contact_list.setModel(self.contact_model)
        self.search_list.setModel(self.contact_search_model)

        self.contact_list.selectionModel().selectionChanged.connect(self._SH_ContactListSelectionChanged)
        self.search_list.selectionModel().selectionChanged.connect(self._SH_SearchListSelectionChanged)
        self.search_box.textChanged.connect(self.contact_search_model.setFilterFixedString)

        self.contact_model.load()

        self.about_panel = AboutPanel(self)
        self.add_account_dialog = AddAccountDialog(self)
        self.contact_editor_dialog = ContactEditorDialog(self.contact_model, self)

        self.session_model = SessionModel(self)
        self.session_list.setModel(self.session_model)

        self.session_list.selectionModel().selectionChanged.connect(self._SH_SessionListSelectionChanged)

        self.main_view.setCurrentWidget(self.contacts_panel)
        self.contacts_view.setCurrentWidget(self.contact_list_panel)
        self.search_view.setCurrentWidget(self.search_list_panel)

        self.conference_button.setEnabled(False)
        self.hangup_all_button.setEnabled(False)

        self.switch_view_button.viewChanged.connect(self._SH_SwitchViewButtonChangedView)

        self.search_box.textChanged.connect(self._SH_SearchBoxTextChanged)
        self.contact_model.itemsAdded.connect(self._SH_ContactModelAddedItems)
        self.contact_model.itemsRemoved.connect(self._SH_ContactModelRemovedItems)

        self.back_to_contacts_button.clicked.connect(self.search_box.clear) # this can be set in designer -Dan

        self.add_contact_button.clicked.connect(self._SH_AddContactButtonClicked)
        self.add_search_contact_button.clicked.connect(self._SH_AddContactButtonClicked)

        self.identity.activated[int].connect(self._SH_IdentityChanged)
        self.identity.currentIndexChanged[int].connect(self._SH_IdentityCurrentIndexChanged)

        self.display_name.editingFinished.connect(self._SH_DisplayNameEditingFinished)
        self.status.activated[int].connect(self._SH_StatusChanged)

        self.silent_button.clicked.connect(self._SH_SilentButtonClicked)

        self.audio_call_button.clicked.connect(self._SH_AudioCallButtonClicked)
        self.contact_list.doubleClicked.connect(self._SH_ContactDoubleClicked) # activated is emitted on single click
        self.search_list.doubleClicked.connect(self._SH_ContactDoubleClicked) # activated is emitted on single click
        self.search_box.returnPressed.connect(self._SH_SearchBoxReturnPressed)

        self.session_model.sessionAdded.connect(self._SH_SessionModelAddedSession)
        self.session_model.structureChanged.connect(self._SH_SessionModelChangedStructure)
        self.hangup_all_button.clicked.connect(self._SH_HangupAllButtonClicked)
        self.conference_button.makeConference.connect(self._SH_MakeConference)
        self.conference_button.breakConference.connect(self._SH_BreakConference)
        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)

        self.search_box.shortcut = QShortcut(self.search_box)
        self.search_box.shortcut.setKey('CTRL+F')
        self.search_box.shortcut.activated.connect(self.search_box.setFocus)

        self.about_action.triggered.connect(self.about_panel.show)
        self.add_account_action.triggered.connect(self.add_account_dialog.open_for_add)
        self.mute_action.triggered.connect(self._SH_MuteButtonClicked)
        self.redial_action.triggered.connect(self._SH_RedialActionTriggered)
        self.silent_action.triggered.connect(self._SH_SilentButtonClicked)
        self.quit_action.triggered.connect(self.close)

        self.idle_status_index = 0

        self.output_devices_group = QActionGroup(self)
        self.input_devices_group = QActionGroup(self)
        self.alert_devices_group = QActionGroup(self)
        self.output_devices_group.triggered.connect(self._SH_AudioOutputDeviceChanged)
        self.input_devices_group.triggered.connect(self._SH_AudioInputDeviceChanged)
        self.alert_devices_group.triggered.connect(self._SH_AudioAlertDeviceChanged)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationWillStart')

    def setupUi(self):
        super(MainWindow, self).setupUi(self)

        # adjust search box height depending on theme as the value set in designer isn't suited for all themes
        search_box = self.search_box
        option = QStyleOptionFrameV2()
        search_box.initStyleOption(option)
        frame_width = search_box.style().pixelMetric(QStyle.PM_DefaultFrameWidth, option, search_box)
        if frame_width < 4:
            search_box.setMinimumHeight(20 + 2*frame_width)

        # adjust status combo-box font size to fit the combo-box
        option = QStyleOptionComboBox()
        self.status.initStyleOption(option)
        frame_width = self.status.style().pixelMetric(QStyle.PM_DefaultFrameWidth, option, self.status)
        font = self.status.font()
        font.setFamily('Sans Serif')
        font.setPointSize(font.pointSize() - 1) # make it 1 point smaller then the default font size
        font_metrics = QFontMetrics(font)
        if font_metrics.height() > self.status.maximumHeight() - 2*frame_width:
            pixel_size = 11 - (frame_width - 2) # subtract 1 pixel for every frame pixel over 2 pixels
            font.setPixelSize(pixel_size)
        self.status.setFont(font)

        # adjust the combo boxes for themes with too much padding (like the default theme on Ubuntu 10.04)
        option = QStyleOptionComboBox()
        self.status.initStyleOption(option)
        font_metrics = self.status.fontMetrics()
        text_width = max(font_metrics.width(self.status.itemText(index)) for index in xrange(self.status.count()))
        frame_width = self.status.style().pixelMetric(QStyle.PM_ComboBoxFrameWidth, option, self.status)
        arrow_width = self.status.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, self.status).width()
        wide_padding = self.status.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, self.status).height() < 10
        self.status.setFixedWidth(text_width + arrow_width + 2*frame_width + 30) # 30? Don't ask.
        self.status.setStyleSheet("""QComboBox { padding: 0px 3px 0px 3px; }""" if wide_padding else "")
        self.identity.setStyleSheet("""QComboBox { padding: 0px 4px 0px 4px; }""" if wide_padding else "")

    def closeEvent(self, event):
        super(MainWindow, self).closeEvent(event)
        self.about_panel.close()
        self.add_account_dialog.close()
        self.contact_editor_dialog.close()

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

    def load_audio_devices(self):
        settings = SIPSimpleSettings()

        action = QAction(u'System default', self.output_devices_group)
        action.setData(QVariant(u'system_default'))
        self.output_device_menu.addAction(action)
        self.output_device_menu.addSeparator()
        for device in SIPApplication.engine.output_devices:
            action = QAction(device, self.output_devices_group)
            action.setData(QVariant(device))
            self.output_device_menu.addAction(action)
        action = QAction(u'None', self.output_devices_group)
        action.setData(QVariant(None))
        self.output_device_menu.addAction(action)
        for action in self.output_devices_group.actions():
            action.setCheckable(True)
            if settings.audio.output_device == action.data().toPyObject():
                action.setChecked(True)

        action = QAction(u'System default', self.input_devices_group)
        action.setData(QVariant(u'system_default'))
        self.input_device_menu.addAction(action)
        self.input_device_menu.addSeparator()
        for device in SIPApplication.engine.input_devices:
            action = QAction(device, self.input_devices_group)
            action.setData(QVariant(device))
            self.input_device_menu.addAction(action)
        action = QAction(u'None', self.input_devices_group)
        action.setData(QVariant(None))
        self.input_device_menu.addAction(action)
        for action in self.input_devices_group.actions():
            action.setCheckable(True)
            if settings.audio.input_device == action.data().toPyObject():
                action.setChecked(True)

        action = QAction(u'System default', self.alert_devices_group)
        action.setData(QVariant(u'system_default'))
        self.alert_device_menu.addAction(action)
        self.alert_device_menu.addSeparator()
        for device in SIPApplication.engine.output_devices:
            action = QAction(device, self.alert_devices_group)
            action.setData(QVariant(device))
            self.alert_device_menu.addAction(action)
        action = QAction(u'None', self.alert_devices_group)
        action.setData(QVariant(None))
        self.alert_device_menu.addAction(action)
        for action in self.alert_devices_group.actions():
            action.setCheckable(True)
            if settings.audio.alert_device == action.data().toPyObject():
                action.setChecked(True)

    def _AH_AccountActionTriggered(self, action, enabled):
        account = action.data().toPyObject()
        account.enabled = enabled
        account.save()

    def _SH_AddContactButtonClicked(self, clicked):
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
        self.contact_editor_dialog.open_for_add(self.search_box.text(), preferred_group)

    def _SH_AudioCallButtonClicked(self):
        list_view = self.contact_list if self.contacts_view.currentWidget() is self.contact_list_panel else self.search_list
        selected_indexes = list_view.selectionModel().selectedIndexes()
        contact = list_view.model().data(selected_indexes[0]) if selected_indexes else Null
        address = contact.uri or unicode(self.search_box.text())
        name = contact.name or None
        session_manager = SessionManager()
        session_manager.start_call(name, address, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)

    def _SH_AudioAlertDeviceChanged(self, action):
        settings = SIPSimpleSettings()
        settings.audio.alert_device = action.data().toPyObject()
        call_in_auxiliary_thread(settings.save)

    def _SH_AudioInputDeviceChanged(self, action):
        settings = SIPSimpleSettings()
        settings.audio.input_device = action.data().toPyObject()
        call_in_auxiliary_thread(settings.save)

    def _SH_AudioOutputDeviceChanged(self, action):
        settings = SIPSimpleSettings()
        settings.audio.output_device = action.data().toPyObject()
        call_in_auxiliary_thread(settings.save)

    def _SH_BreakConference(self):
        active_session = self.session_model.data(self.session_list.selectionModel().selectedIndexes()[0])
        self.session_model.breakConference(active_session.conference)

    def _SH_ContactDoubleClicked(self, index):
        contact = index.model().data(index)
        if not isinstance(contact, Contact):
            return
        session_manager = SessionManager()
        session_manager.start_call(contact.name, contact.uri, contact=contact, account=BonjourAccount() if isinstance(contact, BonjourNeighbour) else None)

    def _SH_ContactListSelectionChanged(self, selected, deselected):
        account_manager = AccountManager()
        selected_items = self.contact_list.selectionModel().selectedIndexes()
        self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)==1 and isinstance(self.contact_model.data(selected_items[0]), Contact))

    def _SH_ContactModelAddedItems(self, items):
        if self.search_box.text().isEmpty():
            return
        active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
        self.search_view.setCurrentWidget(active_widget)

    def _SH_ContactModelRemovedItems(self, items):
        if self.search_box.text().isEmpty():
            return
        if any(type(item) is Contact for item in items) and self.contact_search_model.rowCount() == 0:
            self.search_box.clear()
        else:
            active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
            self.search_view.setCurrentWidget(active_widget)

    def _SH_DisplayNameEditingFinished(self):
        self.display_name.clearFocus()
        index = self.identity.currentIndex()
        if index != -1:
            name = unicode(self.display_name.text())
            account = self.identity.itemData(index).toPyObject().account
            account.display_name = name if name else None
            account.save()

    def _SH_HangupAllButtonClicked(self):
        for session in self.session_model.sessions:
            session.end()

    def _SH_IdentityChanged(self, index):
        account_manager = AccountManager()
        account_manager.default_account = self.identity.itemData(index).toPyObject().account

    def _SH_IdentityCurrentIndexChanged(self, index):
        if index != -1:
            account = self.identity.itemData(index).toPyObject().account
            self.display_name.setText(account.display_name or u'')
            self.display_name.setEnabled(True)
            self.activity_note.setEnabled(True)
            self.status.setEnabled(True)
            if not self.session_model.active_sessions:
                self.status.setCurrentIndex(self.idle_status_index)
        else:
            self.display_name.clear()
            self.display_name.setEnabled(False)
            self.activity_note.setEnabled(False)
            self.status.setEnabled(False)
            self.status.setCurrentIndex(self.status.findText(u'Offline'))

    def _SH_MakeConference(self):
        self.session_model.conferenceSessions([session for session in self.session_model.active_sessions if session.conference is None])

    def _SH_MuteButtonClicked(self, muted):
        self.mute_action.setChecked(muted)
        self.mute_button.setChecked(muted)
        SIPApplication.voice_audio_bridge.mixer.muted = muted

    def _SH_RedialActionTriggered(self):
        session_manager = SessionManager()
        if session_manager.last_dialed_uri is not None:
            session_manager.start_call(None, unicode(session_manager.last_dialed_uri))

    def _SH_SearchBoxReturnPressed(self):
        address = unicode(self.search_box.text())
        if address:
            session_manager = SessionManager()
            session_manager.start_call(None, address)

    def _SH_SearchBoxTextChanged(self, text):
        account_manager = AccountManager()
        if text:
            self.switch_view_button.view = SwitchViewButton.ContactView
            selected_items = self.search_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)<=1)
        else:
            selected_items = self.contact_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)==1 and type(self.contact_model.data(selected_items[0])) is Contact)
        active_widget = self.contact_list_panel if text.isEmpty() else self.search_panel
        if active_widget is self.search_panel and self.contacts_view.currentWidget() is not self.search_panel:
            self.search_list.selectionModel().clearSelection()
        self.contacts_view.setCurrentWidget(active_widget)
        active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
        self.search_view.setCurrentWidget(active_widget)

    def _SH_SearchListSelectionChanged(self, selected, deselected):
        account_manager = AccountManager()
        selected_items = self.search_list.selectionModel().selectedIndexes()
        self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)<=1)

    def _SH_SessionListSelectionChanged(self, selected, deselected):
        selected_indexes = selected.indexes()
        active_session = self.session_model.data(selected_indexes[0]) if selected_indexes else Null
        if active_session.conference:
            self.conference_button.setEnabled(True)
            self.conference_button.setChecked(True)
        else:
            self.conference_button.setEnabled(len([session for session in self.session_model.active_sessions if session.conference is None]) > 1)
            self.conference_button.setChecked(False)

    def _SH_SessionModelAddedSession(self, session_item):
        if session_item.session.state is None:
            self.search_box.clear()

    def _SH_SessionModelChangedStructure(self):
        active_sessions = self.session_model.active_sessions
        self.active_sessions_label.setText(u'There is 1 active call' if len(active_sessions)==1 else u'There are %d active calls' % len(active_sessions))
        self.active_sessions_label.setVisible(any(active_sessions))
        self.hangup_all_button.setEnabled(any(active_sessions))
        selected_indexes = self.session_list.selectionModel().selectedIndexes()
        active_session = self.session_model.data(selected_indexes[0]) if selected_indexes else Null
        if active_session.conference:
            self.conference_button.setEnabled(True)
            self.conference_button.setChecked(True)
        else:
            self.conference_button.setEnabled(len([session for session in active_sessions if session.conference is None]) > 1)
            self.conference_button.setChecked(False)
        if active_sessions and self.status.currentText() != u'Offline':
            self.status.setCurrentIndex(self.status.findText(u'On the phone'))
        else:
            self.status.setCurrentIndex(self.idle_status_index)

    def _SH_SilentButtonClicked(self, silent):
        settings = SIPSimpleSettings()
        settings.audio.silent = silent
        settings.save()

    def _SH_StatusChanged(self, index):
        self.idle_status_index = index

    def _SH_SwitchViewButtonChangedView(self, view):
        self.main_view.setCurrentWidget(self.contacts_panel if view is SwitchViewButton.ContactView else self.sessions_panel)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        settings = SIPSimpleSettings()
        account_manager = AccountManager()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=notification.sender)
        notification_center.add_observer(self, sender=account_manager)
        self.silent_action.setChecked(settings.audio.silent)
        self.silent_button.setChecked(settings.audio.silent)
        if all(not account.enabled for account in account_manager.iter_accounts()):
            self.display_name.setEnabled(False)
            self.activity_note.setEnabled(False)
            self.status.setEnabled(False)
            self.status.setCurrentIndex(self.status.findText(u'Offline'))
        for account in account_manager.iter_accounts():
            action = QAction(account.id if account is not BonjourAccount() else u'Bonjour', None)
            action.setCheckable(True)
            action.setData(QVariant(account))
            action.setChecked(account.enabled)
            action.triggered.connect(partial(self._AH_AccountActionTriggered, action))
            self.accounts_menu.addAction(action)

    def _NH_SIPApplicationDidStart(self, notification):
        self.load_audio_devices()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='AudioDevicesDidChange')

    def _NH_AudioDevicesDidChange(self, notification):
        for action in self.output_device_menu.actions():
            self.output_devices_group.removeAction(action)
            self.output_device_menu.removeAction(action)
        for action in self.input_device_menu.actions():
            self.input_devices_group.removeAction(action)
            self.input_device_menu.removeAction(action)
        for action in self.alert_device_menu.actions():
            self.alert_devices_group.removeAction(action)
            self.alert_device_menu.removeAction(action)
        self.load_audio_devices()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.sender is settings:
            if 'audio.silent' in notification.data.modified:
                self.silent_action.setChecked(settings.audio.silent)
                self.silent_button.setChecked(settings.audio.silent)
            if 'audio.output_device' in notification.data.modified:
                action = (action for action in self.output_devices_group.actions() if action.data().toPyObject() == settings.audio.output_device).next()
                action.setChecked(True)
            if 'audio.input_device' in notification.data.modified:
                action = (action for action in self.input_devices_group.actions() if action.data().toPyObject() == settings.audio.input_device).next()
                action.setChecked(True)
            if 'audio.alert_device' in notification.data.modified:
                action = (action for action in self.alert_devices_group.actions() if action.data().toPyObject() == settings.audio.alert_device).next()
                action.setChecked(True)
        elif isinstance(notification.sender, Account) or notification.sender is BonjourAccount():
            if 'enabled' in notification.data.modified:
                account = notification.sender
                action = (action for action in self.accounts_menu.actions() if action.data().toPyObject() is account).next()
                action.setChecked(account.enabled)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        action = QAction(account.id, None)
        action.setCheckable(True)
        action.setData(QVariant(account))
        action.triggered.connect(partial(self._AH_AccountActionTriggered, action))
        self.accounts_menu.addAction(action)

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        account = notification.data.account
        action = (action for action in self.accounts_menu.actions() if action.data().toPyObject() is account).next()
        self.account_menu.removeAction(action)

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        if notification.data.account is None:
            self.enable_call_buttons(False)
        else:
            selected_items = self.contact_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(len(selected_items)==1 and isinstance(self.contact_model.data(selected_items[0]), Contact))

del ui_class, base_class


