# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['MainWindow']

import hashlib
import os

from functools import partial

from PyQt4 import uic
from PyQt4.QtCore import Qt, QSettings, QUrl
from PyQt4.QtGui  import QAction, QActionGroup, QDesktopServices, QMenu, QShortcut
from PyQt4.QtGui  import QApplication, QFileDialog, QIcon, QStyle, QStyleOptionComboBox, QStyleOptionFrameV2, QSystemTrayIcon

from application.notification import IObserver, NotificationCenter
from application.python import Null, limit
from application.system import makedirs
from zope.interface import implements

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings

from blink.aboutpanel import AboutPanel
from blink.accounts import AccountModel, ActiveAccountModel, ServerToolsAccountModel, ServerToolsWindow
from blink.contacts import Contact, ContactEditorDialog, ContactModel, ContactSearchModel, GoogleContactsDialog, URIUtils
from blink.filetransferwindow import FileTransferWindow
from blink.history import HistoryManager
from blink.preferences import PreferencesWindow
from blink.sessions import ConferenceDialog, SessionManager, AudioSessionModel, StreamDescription
from blink.configuration.datatypes import IconDescriptor, FileURL, InvalidToken, PresenceState
from blink.configuration.settings import BlinkSettings
from blink.presence import PendingWatcherDialog
from blink.resources import ApplicationData, IconManager, Resources
from blink.util import run_in_gui_thread
from blink.widgets.buttons import AccountState, SwitchViewButton


ui_class, base_class = uic.loadUiType(Resources.get('blink.ui'))

class MainWindow(base_class, ui_class):
    implements(IObserver)

    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)
        self.saved_account_state = None

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationWillStart')
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='SIPAccountGotMessageSummary')
        notification_center.add_observer(self, name='SIPAccountGotPendingWatcher')
        notification_center.add_observer(self, name='BlinkSessionNewOutgoing')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForOutgoing')
        notification_center.add_observer(self, name='FileTransferNewIncoming')
        notification_center.add_observer(self, name='FileTransferNewOutgoing')
        notification_center.add_observer(self, sender=AccountManager())

        icon_manager = IconManager()

        self.pending_watcher_dialogs = []

        self.mwi_icons = [QIcon(Resources.get('icons/mwi-%d.png' % i)) for i in xrange(0, 11)]
        self.mwi_icons.append(QIcon(Resources.get('icons/mwi-many.png')))

        with Resources.directory:
            self.setupUi()

        self.setWindowTitle('Blink')
        self.setWindowIconText('Blink')

        geometry = QSettings().value("main_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        self.default_icon_path = Resources.get('icons/default-avatar.png')
        self.default_icon = QIcon(self.default_icon_path)
        self.last_icon_directory = os.path.expanduser('~')
        self.set_user_icon(icon_manager.get('avatar'))

        self.active_sessions_label.hide()
        self.enable_call_buttons(False)
        self.conference_button.setEnabled(False)
        self.hangup_all_button.setEnabled(False)
        self.sip_server_settings_action.setEnabled(False)
        self.search_for_people_action.setEnabled(False)
        self.history_on_server_action.setEnabled(False)
        self.buy_pstn_access_action.setEnabled(False)
        self.main_view.setCurrentWidget(self.contacts_panel)
        self.contacts_view.setCurrentWidget(self.contact_list_panel)
        self.search_view.setCurrentWidget(self.search_list_panel)

        # System tray
        if QSystemTrayIcon.isSystemTrayAvailable() and not os.getenv('XDG_CURRENT_DESKTOP', '').lower().startswith('unity'):
            self.system_tray_icon = QSystemTrayIcon(QIcon(Resources.get('icons/blink.png')), self)
            self.system_tray_icon.activated.connect(self._SH_SystemTrayIconActivated)
            menu = QMenu(self)
            menu.addAction(QAction("Show", self, triggered=self._AH_SystemTrayShowWindow))
            menu.addAction(QAction(QIcon(Resources.get('icons/application-exit.png')), "Quit", self, triggered=self._AH_QuitActionTriggered))
            self.system_tray_icon.setContextMenu(menu)
            self.system_tray_icon.show()
        else:
            self.system_tray_icon = None

        # Accounts
        self.account_model = AccountModel(self)
        self.enabled_account_model = ActiveAccountModel(self.account_model, self)
        self.server_tools_account_model = ServerToolsAccountModel(self.account_model, self)
        self.identity.setModel(self.enabled_account_model)

        # Contacts
        self.contact_model = ContactModel(self)
        self.contact_search_model = ContactSearchModel(self.contact_model, self)
        self.contact_list.setModel(self.contact_model)
        self.search_list.setModel(self.contact_search_model)

        # Sessions (audio)
        self.session_model = AudioSessionModel(self)
        self.session_list.setModel(self.session_model)
        self.session_list.selectionModel().selectionChanged.connect(self._SH_SessionListSelectionChanged)

        # Windows, dialogs and panels
        self.about_panel = AboutPanel(self)
        self.conference_dialog = ConferenceDialog(self)
        self.contact_editor_dialog = ContactEditorDialog(self)
        self.google_contacts_dialog = GoogleContactsDialog(self)
        self.filetransfer_window = FileTransferWindow()
        self.preferences_window = PreferencesWindow(self.account_model, None)
        self.server_tools_window = ServerToolsWindow(self.server_tools_account_model, None)

        # Signals
        self.account_state.stateChanged.connect(self._SH_AccountStateChanged)
        self.account_state.clicked.connect(self._SH_AccountStateClicked)
        self.activity_note.editingFinished.connect(self._SH_ActivityNoteEditingFinished)
        self.add_contact_button.clicked.connect(self._SH_AddContactButtonClicked)
        self.add_search_contact_button.clicked.connect(self._SH_AddContactButtonClicked)
        self.audio_call_button.clicked.connect(self._SH_AudioCallButtonClicked)
        self.chat_session_button.clicked.connect(self._SH_ChatSessionButtonClicked)
        self.back_to_contacts_button.clicked.connect(self.search_box.clear) # this can be set in designer -Dan
        self.conference_button.makeConference.connect(self._SH_MakeConference)
        self.conference_button.breakConference.connect(self._SH_BreakConference)

        self.contact_list.selectionModel().selectionChanged.connect(self._SH_ContactListSelectionChanged)
        self.contact_model.itemsAdded.connect(self._SH_ContactModelAddedItems)
        self.contact_model.itemsRemoved.connect(self._SH_ContactModelRemovedItems)

        self.display_name.editingFinished.connect(self._SH_DisplayNameEditingFinished)
        self.hangup_all_button.clicked.connect(self._SH_HangupAllButtonClicked)

        self.identity.activated[int].connect(self._SH_IdentityChanged)
        self.identity.currentIndexChanged[int].connect(self._SH_IdentityCurrentIndexChanged)

        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)

        self.search_box.textChanged.connect(self._SH_SearchBoxTextChanged)
        self.search_box.returnPressed.connect(self._SH_SearchBoxReturnPressed)
        self.search_box.shortcut.activated.connect(self.search_box.setFocus)

        self.search_list.selectionModel().selectionChanged.connect(self._SH_SearchListSelectionChanged)

        self.server_tools_account_model.rowsInserted.connect(self._SH_ServerToolsAccountModelChanged)
        self.server_tools_account_model.rowsRemoved.connect(self._SH_ServerToolsAccountModelChanged)

        self.session_model.sessionAdded.connect(self._SH_AudioSessionModelAddedSession)
        self.session_model.sessionRemoved.connect(self._SH_AudioSessionModelRemovedSession)
        self.session_model.structureChanged.connect(self._SH_AudioSessionModelChangedStructure)

        self.silent_button.clicked.connect(self._SH_SilentButtonClicked)
        self.switch_view_button.viewChanged.connect(self._SH_SwitchViewButtonChangedView)

        # Blink menu actions
        self.about_action.triggered.connect(self.about_panel.show)
        self.add_account_action.triggered.connect(self.preferences_window.show_add_account_dialog)
        self.manage_accounts_action.triggered.connect(self.preferences_window.show_for_accounts)
        self.help_action.triggered.connect(partial(QDesktopServices.openUrl, QUrl(u'http://icanblink.com/help-qt.phtml')))
        self.preferences_action.triggered.connect(self.preferences_window.show)
        self.auto_accept_chat_action.triggered.connect(self._AH_AutoAcceptChatTriggered)
        self.release_notes_action.triggered.connect(partial(QDesktopServices.openUrl, QUrl(u'http://icanblink.com/changelog-qt.phtml')))
        self.quit_action.triggered.connect(self._AH_QuitActionTriggered)

        # Call menu actions
        self.redial_action.triggered.connect(self._AH_RedialActionTriggered)
        self.join_conference_action.triggered.connect(self.conference_dialog.show)
        self.mute_action.triggered.connect(self._SH_MuteButtonClicked)
        self.silent_action.triggered.connect(self._SH_SilentButtonClicked)

        # Devices menu actions
        self.output_devices_group.triggered.connect(self._AH_AudioOutputDeviceChanged)
        self.input_devices_group.triggered.connect(self._AH_AudioInputDeviceChanged)
        self.alert_devices_group.triggered.connect(self._AH_AudioAlertDeviceChanged)

        # History menu actions
        self.history_manager = HistoryManager()
        self.history_menu.aboutToShow.connect(self._SH_HistoryMenuAboutToShow)
        self.history_menu.triggered.connect(self._AH_HistoryMenuTriggered)

        # Tools menu actions
        self.answering_machine_action.triggered.connect(self._AH_EnableAnsweringMachineTriggered)
        self.sip_server_settings_action.triggered.connect(self._AH_SIPServerSettings)
        self.search_for_people_action.triggered.connect(self._AH_SearchForPeople)
        self.history_on_server_action.triggered.connect(self._AH_HistoryOnServer)
        self.buy_pstn_access_action.triggered.connect(self._AH_PurchasePstnAccess)
        self.file_transfers_action.triggered.connect(self._AH_FileTransfersActionTriggered)
        self.logs_action.triggered.connect(self._AH_LogsActionTriggered)

    def setupUi(self):
        super(MainWindow, self).setupUi(self)

        self.search_box.shortcut = QShortcut(self.search_box)
        self.search_box.shortcut.setKey('Ctrl+F')

        self.output_devices_group = QActionGroup(self)
        self.input_devices_group = QActionGroup(self)
        self.alert_devices_group = QActionGroup(self)

        # adjust search box height depending on theme as the value set in designer isn't suited for all themes
        search_box = self.search_box
        option = QStyleOptionFrameV2()
        search_box.initStyleOption(option)
        frame_width = search_box.style().pixelMetric(QStyle.PM_DefaultFrameWidth, option, search_box)
        if frame_width < 4:
            search_box.setMinimumHeight(20 + 2*frame_width)

        # adjust the combo boxes for themes with too much padding (like the default theme on Ubuntu 10.04)
        option = QStyleOptionComboBox()
        self.identity.initStyleOption(option)
        wide_padding = self.identity.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, self.identity).height() < 10
        self.identity.setStyleSheet("""QComboBox { padding: 0px 4px 0px 4px; }""" if wide_padding else "")

    def closeEvent(self, event):
        QSettings().setValue("main_window/geometry", self.saveGeometry())
        super(MainWindow, self).closeEvent(event)
        self.about_panel.close()
        self.conference_dialog.close()
        self.contact_editor_dialog.close()
        self.google_contacts_dialog.close()
        self.filetransfer_window.close()
        self.preferences_window.close()
        self.server_tools_window.close()
        for dialog in self.pending_watcher_dialogs[:]:
            dialog.close()

    def set_user_icon(self, icon):
        self.account_state.setIcon(icon or self.default_icon)

    def enable_call_buttons(self, enabled):
        self.audio_call_button.setEnabled(enabled)
        self.chat_session_button.setEnabled(enabled)
        self.screen_sharing_button.setEnabled(False)

    def load_audio_devices(self):
        settings = SIPSimpleSettings()

        action = QAction(u'System default', self.output_devices_group)
        action.setData(u'system_default')
        self.output_device_menu.addAction(action)
        self.output_device_menu.addSeparator()
        for device in SIPApplication.engine.output_devices:
            action = QAction(device, self.output_devices_group)
            action.setData(device)
            self.output_device_menu.addAction(action)
        action = QAction(u'None', self.output_devices_group)
        action.setData(None)
        self.output_device_menu.addAction(action)
        for action in self.output_devices_group.actions():
            action.setCheckable(True)
            if settings.audio.output_device == action.data():
                action.setChecked(True)

        action = QAction(u'System default', self.input_devices_group)
        action.setData(u'system_default')
        self.input_device_menu.addAction(action)
        self.input_device_menu.addSeparator()
        for device in SIPApplication.engine.input_devices:
            action = QAction(device, self.input_devices_group)
            action.setData(device)
            self.input_device_menu.addAction(action)
        action = QAction(u'None', self.input_devices_group)
        action.setData(None)
        self.input_device_menu.addAction(action)
        for action in self.input_devices_group.actions():
            action.setCheckable(True)
            if settings.audio.input_device == action.data():
                action.setChecked(True)

        action = QAction(u'System default', self.alert_devices_group)
        action.setData(u'system_default')
        self.alert_device_menu.addAction(action)
        self.alert_device_menu.addSeparator()
        for device in SIPApplication.engine.output_devices:
            action = QAction(device, self.alert_devices_group)
            action.setData(device)
            self.alert_device_menu.addAction(action)
        action = QAction(u'None', self.alert_devices_group)
        action.setData(None)
        self.alert_device_menu.addAction(action)
        for action in self.alert_devices_group.actions():
            action.setCheckable(True)
            if settings.audio.alert_device == action.data():
                action.setChecked(True)

    def _AH_AccountActionTriggered(self, action, enabled):
        account = action.data()
        account.enabled = enabled
        account.save()

    def _AH_AudioAlertDeviceChanged(self, action):
        settings = SIPSimpleSettings()
        settings.audio.alert_device = action.data()
        settings.save()

    def _AH_AudioInputDeviceChanged(self, action):
        settings = SIPSimpleSettings()
        settings.audio.input_device = action.data()
        settings.save()

    def _AH_AudioOutputDeviceChanged(self, action):
        settings = SIPSimpleSettings()
        settings.audio.output_device = action.data()
        settings.save()

    def _AH_AutoAcceptChatTriggered(self, checked):
        settings = SIPSimpleSettings()
        settings.chat.auto_accept = checked
        settings.save()

    def _AH_EnableAnsweringMachineTriggered(self, checked):
        settings = SIPSimpleSettings()
        settings.answering_machine.enabled = checked
        settings.save()

    def _AH_GoogleContactsActionTriggered(self):
        settings = SIPSimpleSettings()
        if settings.google_contacts.authorization_token is not None:
            settings.google_contacts.authorization_token = None
            settings.save()
            self.google_contacts_dialog.hide()
        else:
            self.google_contacts_dialog.open()

    def _AH_RedialActionTriggered(self):
        session_manager = SessionManager()
        if session_manager.last_dialed_uri is not None:
            contact, contact_uri = URIUtils.find_contact(session_manager.last_dialed_uri)
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio')]) # TODO: remember used media types and redial with them. -Saul

    def _AH_SIPServerSettings(self, checked):
        account = self.identity.itemData(self.identity.currentIndex()).account
        account = account if account is not BonjourAccount() and account.server.settings_url else None
        self.server_tools_window.open_settings_page(account)

    def _AH_SearchForPeople(self, checked):
        account = self.identity.itemData(self.identity.currentIndex()).account
        account = account if account is not BonjourAccount() and account.server.settings_url else None
        self.server_tools_window.open_search_for_people_page(account)

    def _AH_HistoryOnServer(self, checked):
        account = self.identity.itemData(self.identity.currentIndex()).account
        account = account if account is not BonjourAccount() and account.server.settings_url else None
        self.server_tools_window.open_history_page(account)

    def _AH_PurchasePstnAccess(self, checked):
        account = self.identity.itemData(self.identity.currentIndex()).account
        account = account if account is not BonjourAccount() and account.server.settings_url else None
        self.server_tools_window.open_buy_pstn_access_page(account)

    def _AH_FileTransfersActionTriggered(self, checked):
        self.filetransfer_window.show()

    def _AH_LogsActionTriggered(self, checked):
        directory = ApplicationData.get('logs')
        makedirs(directory)
        QDesktopServices.openUrl(QUrl.fromLocalFile(directory))

    def _AH_VoicemailActionTriggered(self, action, checked):
        account = action.data()
        contact, contact_uri = URIUtils.find_contact(account.voicemail_uri, display_name='Voicemail')
        session_manager = SessionManager()
        session_manager.create_session(contact, contact_uri, [StreamDescription('audio')], account=account)

    def _SH_HistoryMenuAboutToShow(self):
        self.history_menu.clear()
        if self.history_manager.calls:
            for entry in reversed(self.history_manager.calls):
                action = self.history_menu.addAction(entry.icon, entry.text)
                action.entry = entry
                action.setToolTip(entry.uri)
        else:
            action = self.history_menu.addAction("Call history is empty")
            action.setEnabled(False)

    def _AH_HistoryMenuTriggered(self, action):
        account_manager = AccountManager()
        session_manager = SessionManager()
        try:
            account = account_manager.get_account(action.entry.account_id)
        except KeyError:
            account = None
        contact, contact_uri = URIUtils.find_contact(action.entry.uri)
        session_manager.create_session(contact, contact_uri, [StreamDescription('audio')], account=account) # TODO: memorize media type and use it? -Saul (not sure about history in/out -Dan)

    def _AH_SystemTrayShowWindow(self, checked):
        self.show()
        self.raise_()
        self.activateWindow()

    def _AH_QuitActionTriggered(self, checked):
        if self.system_tray_icon is not None:
            self.system_tray_icon.hide()
        QApplication.instance().quit()

    def _SH_AccountStateChanged(self):
        self.activity_note.setText(self.account_state.note)
        if self.account_state.state is AccountState.Invisible:
            self.activity_note.inactiveText = u'(invisible)'
            self.activity_note.setEnabled(False)
        else:
            if not self.activity_note.isEnabled():
                self.activity_note.inactiveText = u'Add an activity note here'
                self.activity_note.setEnabled(True)
        if not self.account_state.state.internal:
            self.saved_account_state = None
        blink_settings = BlinkSettings()
        blink_settings.presence.current_state = PresenceState(self.account_state.state, self.account_state.note)
        blink_settings.presence.state_history = [PresenceState(state, note) for state, note in self.account_state.history]
        blink_settings.save()

    def _SH_AccountStateClicked(self, checked):
        filename = QFileDialog.getOpenFileName(self, u'Select Icon', self.last_icon_directory, u"Images (*.png *.tiff *.jpg *.xmp *.svg)")
        if filename:
            self.last_icon_directory = os.path.dirname(filename)
            filename = filename if os.path.realpath(filename) != os.path.realpath(self.default_icon_path) else None
            blink_settings = BlinkSettings()
            icon_manager = IconManager()
            if filename is not None:
                icon = icon_manager.store_file('avatar', filename)
                if icon is not None:
                    blink_settings.presence.icon = IconDescriptor(FileURL(icon.filename), hashlib.sha1(icon.content).hexdigest())
                else:
                    icon_manager.remove('avatar')
                    blink_settings.presence.icon = None
            else:
                icon_manager.remove('avatar')
                blink_settings.presence.icon = None
            blink_settings.save()

    def _SH_ActivityNoteEditingFinished(self):
        self.activity_note.clearFocus()
        note = self.activity_note.text()
        if note != self.account_state.note:
            self.account_state.state.internal = False
            self.account_state.setState(self.account_state.state, note)

    def _SH_AddContactButtonClicked(self, clicked):
        self.contact_editor_dialog.open_for_add(self.search_box.text(), None)

    def _SH_AudioCallButtonClicked(self):
        list_view = self.contact_list if self.contacts_view.currentWidget() is self.contact_list_panel else self.search_list
        if list_view.detail_view.isVisible():
            list_view.detail_view._AH_StartAudioCall()
        else:
            selected_indexes = list_view.selectionModel().selectedIndexes()
            if selected_indexes:
                contact = selected_indexes[0].data(Qt.UserRole)
                contact_uri = contact.uri
            else:
                contact, contact_uri = URIUtils.find_contact(self.search_box.text())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio')])

    def _SH_ChatSessionButtonClicked(self):
        list_view = self.contact_list if self.contacts_view.currentWidget() is self.contact_list_panel else self.search_list
        if list_view.detail_view.isVisible():
            list_view.detail_view._AH_StartChatSession()
        else:
            selected_indexes = list_view.selectionModel().selectedIndexes()
            if selected_indexes:
                contact = selected_indexes[0].data(Qt.UserRole)
                contact_uri = contact.uri
            else:
                contact, contact_uri = URIUtils.find_contact(self.search_box.text())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('chat')], connect=False)

    def _SH_BreakConference(self):
        active_session = self.session_list.selectionModel().selectedIndexes()[0].data(Qt.UserRole)
        self.session_model.breakConference(active_session.client_conference)

    def _SH_ContactListSelectionChanged(self, selected, deselected):
        account_manager = AccountManager()
        selected_items = self.contact_list.selectionModel().selectedIndexes()
        self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)==1 and isinstance(selected_items[0].data(Qt.UserRole), Contact))

    def _SH_ContactModelAddedItems(self, items):
        if not self.search_box.text():
            return
        active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
        self.search_view.setCurrentWidget(active_widget)

    def _SH_ContactModelRemovedItems(self, items):
        if not self.search_box.text():
            return
        if any(type(item) is Contact for item in items) and self.contact_search_model.rowCount() == 0:
            self.search_box.clear() # check this. it is no longer be the correct behaviour as now contacts can be deleted from remote -Dan
        else:
            active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
            self.search_view.setCurrentWidget(active_widget)

    def _SH_DisplayNameEditingFinished(self):
        self.display_name.clearFocus()
        index = self.identity.currentIndex()
        if index != -1:
            name = self.display_name.text()
            account = self.identity.itemData(index).account
            account.display_name = name if name else None
            account.save()

    def _SH_HangupAllButtonClicked(self):
        for session in self.session_model.sessions:
            session.end()

    def _SH_IdentityChanged(self, index):
        account_manager = AccountManager()
        account_manager.default_account = self.identity.itemData(index).account

    def _SH_IdentityCurrentIndexChanged(self, index):
        if index != -1:
            account = self.identity.itemData(index).account
            self.display_name.setText(account.display_name or u'')
            self.display_name.setEnabled(True)
            self.activity_note.setEnabled(True)
            self.account_state.setEnabled(True)
        else:
            self.display_name.clear()
            self.display_name.setEnabled(False)
            self.activity_note.setEnabled(False)
            self.account_state.setEnabled(False)
            self.account_state.setState(AccountState.Invisible)
            self.saved_account_state = None

    def _SH_MakeConference(self):
        self.session_model.conferenceSessions([session for session in self.session_model.active_sessions if session.client_conference is None])

    def _SH_MuteButtonClicked(self, muted):
        settings = SIPSimpleSettings()
        settings.audio.muted = muted
        settings.save()

    def _SH_SearchBoxReturnPressed(self):
        address = self.search_box.text()
        if address:
            contact, contact_uri = URIUtils.find_contact(address)
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio')])

    def _SH_SearchBoxTextChanged(self, text):
        self.contact_search_model.setFilterFixedString(text)
        account_manager = AccountManager()
        if text:
            self.switch_view_button.view = SwitchViewButton.ContactView
            if self.contacts_view.currentWidget() is not self.search_panel:
                self.search_list.selectionModel().clearSelection()
            self.contacts_view.setCurrentWidget(self.search_panel)
            self.search_view.setCurrentWidget(self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel)
            selected_items = self.search_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)<=1)
        else:
            self.contacts_view.setCurrentWidget(self.contact_list_panel)
            selected_items = self.contact_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)==1 and type(selected_items[0].data(Qt.UserRole)) is Contact)
        self.search_list.detail_model.contact = None
        self.search_list.detail_view.hide()

    def _SH_SearchListSelectionChanged(self, selected, deselected):
        account_manager = AccountManager()
        selected_items = self.search_list.selectionModel().selectedIndexes()
        self.enable_call_buttons(account_manager.default_account is not None and len(selected_items)<=1)

    def _SH_ServerToolsAccountModelChanged(self, parent_index, start, end):
        server_tools_enabled = self.server_tools_account_model.rowCount() > 0
        self.sip_server_settings_action.setEnabled(server_tools_enabled)
        self.search_for_people_action.setEnabled(server_tools_enabled)
        self.history_on_server_action.setEnabled(server_tools_enabled)
        self.buy_pstn_access_action.setEnabled(server_tools_enabled)

    def _SH_SessionListSelectionChanged(self, selected, deselected):
        selected_indexes = selected.indexes()
        active_session = selected_indexes[0].data(Qt.UserRole) if selected_indexes else Null
        if active_session.client_conference:
            self.conference_button.setEnabled(True)
            self.conference_button.setChecked(True)
        else:
            self.conference_button.setEnabled(len([session for session in self.session_model.active_sessions if session.client_conference is None]) > 1)
            self.conference_button.setChecked(False)

    def _SH_AudioSessionModelAddedSession(self, session_item):
        if len(session_item.blink_session.streams) == 1:
            self.switch_view_button.view = SwitchViewButton.SessionView

    def _SH_AudioSessionModelRemovedSession(self, session_item):
        if self.session_model.rowCount() == 0:
            self.switch_view_button.view = SwitchViewButton.ContactView

    def _SH_AudioSessionModelChangedStructure(self):
        active_sessions = self.session_model.active_sessions
        self.active_sessions_label.setText(u'There is 1 active call' if len(active_sessions)==1 else u'There are %d active calls' % len(active_sessions))
        self.active_sessions_label.setVisible(any(active_sessions))
        self.hangup_all_button.setEnabled(any(active_sessions))
        selected_indexes = self.session_list.selectionModel().selectedIndexes()
        active_session = selected_indexes[0].data(Qt.UserRole) if selected_indexes else Null
        if active_session.client_conference:
            self.conference_button.setEnabled(True)
            self.conference_button.setChecked(True)
        else:
            self.conference_button.setEnabled(len([session for session in active_sessions if session.client_conference is None]) > 1)
            self.conference_button.setChecked(False)
        if active_sessions:
            if self.account_state.state is not AccountState.Invisible:
                if self.saved_account_state is None:
                    self.saved_account_state = self.account_state.state, self.activity_note.text()
                self.account_state.setState(AccountState.Busy.Internal, note=u'On the phone')
        elif self.saved_account_state is not None:
            state, note = self.saved_account_state
            self.saved_account_state = None
            self.account_state.setState(state, note)

    def _SH_SilentButtonClicked(self, silent):
        settings = SIPSimpleSettings()
        settings.audio.silent = silent
        settings.save()

    def _SH_SwitchViewButtonChangedView(self, view):
        self.main_view.setCurrentWidget(self.contacts_panel if view is SwitchViewButton.ContactView else self.sessions_panel)

    def _SH_PendingWatcherDialogFinished(self, result):
        self.pending_watcher_dialogs.remove(self.sender())

    def _SH_SystemTrayIconActivated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show()
            self.raise_()
            self.activateWindow()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        account_manager = AccountManager()
        settings = SIPSimpleSettings()
        self.silent_action.setChecked(settings.audio.silent)
        self.silent_button.setChecked(settings.audio.silent)
        self.answering_machine_action.setChecked(settings.answering_machine.enabled)
        self.auto_accept_chat_action.setChecked(settings.chat.auto_accept)
        if settings.google_contacts.authorization_token is None:
            self.google_contacts_action.setText(u'Enable Google Contacts')
        else:
            self.google_contacts_action.setText(u'Disable Google Contacts')
        self.google_contacts_action.triggered.connect(self._AH_GoogleContactsActionTriggered)
        if not any(account.enabled for account in account_manager.iter_accounts()):
            self.display_name.setEnabled(False)
            self.activity_note.setEnabled(False)
            self.account_state.setEnabled(False)

    def _NH_SIPApplicationDidStart(self, notification):
        self.load_audio_devices()
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification.center.add_observer(self, name='AudioDevicesDidChange')
        blink_settings = BlinkSettings()
        self.account_state.history = [(item.state, item.note) for item in blink_settings.presence.state_history]
        state = getattr(AccountState, blink_settings.presence.current_state.state, AccountState.Available)
        self.account_state.setState(state, blink_settings.presence.current_state.note)

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
        if self.session_model.active_sessions:
            old_devices = set(notification.data.old_devices)
            new_devices = set(notification.data.new_devices)
            added_devices = new_devices - old_devices
            if added_devices:
                new_device = added_devices.pop()
                settings = SIPSimpleSettings()
                settings.audio.input_device = new_device
                settings.audio.output_device = new_device
                settings.save()
        self.load_audio_devices()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        blink_settings = BlinkSettings()
        icon_manager = IconManager()
        if notification.sender is settings:
            if 'audio.muted' in notification.data.modified:
                self.mute_action.setChecked(settings.audio.muted)
                self.mute_button.setChecked(settings.audio.muted)
            if 'audio.silent' in notification.data.modified:
                self.silent_action.setChecked(settings.audio.silent)
                self.silent_button.setChecked(settings.audio.silent)
            if 'audio.output_device' in notification.data.modified:
                action = (action for action in self.output_devices_group.actions() if action.data() == settings.audio.output_device).next()
                action.setChecked(True)
            if 'audio.input_device' in notification.data.modified:
                action = (action for action in self.input_devices_group.actions() if action.data() == settings.audio.input_device).next()
                action.setChecked(True)
            if 'audio.alert_device' in notification.data.modified:
                action = (action for action in self.alert_devices_group.actions() if action.data() == settings.audio.alert_device).next()
                action.setChecked(True)
            if 'answering_machine.enabled' in notification.data.modified:
                self.answering_machine_action.setChecked(settings.answering_machine.enabled)
            if 'chat.auto_accept' in notification.data.modified:
                self.auto_accept_chat_action.setChecked(settings.chat.auto_accept)
            if 'google_contacts.authorization_token' in notification.data.modified:
                authorization_token = notification.sender.google_contacts.authorization_token
                if authorization_token is None:
                    self.google_contacts_action.setText(u'Enable Google Contacts')
                else:
                    self.google_contacts_action.setText(u'Disable Google Contacts')
                if authorization_token is InvalidToken:
                    self.google_contacts_dialog.open_for_incorrect_password()
        elif notification.sender is blink_settings:
            if 'presence.current_state' in notification.data.modified:
                state = getattr(AccountState, blink_settings.presence.current_state.state, AccountState.Available)
                self.account_state.setState(state, blink_settings.presence.current_state.note)
            if 'presence.icon' in notification.data.modified:
                self.set_user_icon(icon_manager.get('avatar'))
            if 'presence.offline_note' in notification.data.modified:
                # TODO: set offline note -Saul
                pass
        elif isinstance(notification.sender, (Account, BonjourAccount)):
            account_manager = AccountManager()
            account = notification.sender
            if 'enabled' in notification.data.modified:
                action = (action for action in self.accounts_menu.actions() if action.data() is account).next()
                action.setChecked(account.enabled)
            if 'display_name' in notification.data.modified and account is account_manager.default_account:
                self.display_name.setText(account.display_name or u'')
            if set(['enabled', 'message_summary.enabled', 'message_summary.voicemail_uri']).intersection(notification.data.modified):
                action = (action for action in self.voicemail_menu.actions() if action.data() is account).next()
                action.setVisible(False if account is BonjourAccount() else account.enabled and account.message_summary.enabled)
                action.setEnabled(False if account is BonjourAccount() else account.voicemail_uri is not None)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        action = QAction(account.id if account is not BonjourAccount() else u'Bonjour', None)
        action.setEnabled(True if account is not BonjourAccount() else BonjourAccount.mdns_available)
        action.setCheckable(True)
        action.setChecked(account.enabled)
        action.setData(account)
        action.triggered.connect(partial(self._AH_AccountActionTriggered, action))
        self.accounts_menu.addAction(action)
        action = QAction(self.mwi_icons[0], account.id, None)
        action.setVisible(False if account is BonjourAccount() else account.enabled and account.message_summary.enabled)
        action.setEnabled(False if account is BonjourAccount() else account.voicemail_uri is not None)
        action.setData(account)
        action.triggered.connect(partial(self._AH_VoicemailActionTriggered, action))
        self.voicemail_menu.addAction(action)

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        account = notification.data.account
        action = (action for action in self.accounts_menu.actions() if action.data() is account).next()
        self.accounts_menu.removeAction(action)
        action = (action for action in self.voicemail_menu.actions() if action.data() is account).next()
        self.voicemail_menu.removeAction(action)

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        if notification.data.account is None:
            self.enable_call_buttons(False)
        else:
            selected_items = self.contact_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(len(selected_items)==1 and isinstance(selected_items[0].data(Qt.UserRole), Contact))

    def _NH_SIPAccountGotMessageSummary(self, notification):
        account = notification.sender
        summary = notification.data.message_summary
        action = (action for action in self.voicemail_menu.actions() if action.data() is account).next()
        action.setEnabled(account.voicemail_uri is not None)
        if summary.messages_waiting:
            try:
                new_messages = limit(int(summary.summaries['voice-message']['new_messages']), min=0, max=11)
            except (KeyError, ValueError):
                new_messages = 0
        else:
            new_messages = 0
        action.setIcon(self.mwi_icons[new_messages])

    def _NH_SIPAccountGotPendingWatcher(self, notification):
        dialog = PendingWatcherDialog(notification.sender, notification.data.uri, notification.data.display_name)
        dialog.finished.connect(self._SH_PendingWatcherDialogFinished)
        self.pending_watcher_dialogs.append(dialog)
        dialog.show()

    def _NH_BlinkSessionNewOutgoing(self, notification):
        self.search_box.clear()

    def _NH_BlinkSessionDidReinitializeForOutgoing(self, notification):
        self.search_box.clear()

    def _NH_FileTransferNewIncoming(self, notification):
        self.filetransfer_window.show(activate=QApplication.activeWindow() is not None)

    def _NH_FileTransferNewOutgoing(self, notification):
        self.filetransfer_window.show(activate=QApplication.activeWindow() is not None)

del ui_class, base_class


