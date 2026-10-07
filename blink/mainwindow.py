
import hashlib
import os

from functools import partial

from PyQt6 import uic
from PyQt6.QtCore import Qt, QRectF, QSettings, QSize, QTimer, QUrl, QTranslator
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QAction, QActionGroup, QPainter, QShortcut
from PyQt6.QtWidgets import QApplication, QFileDialog, QMenu, QStyle, QStyleOptionComboBox, QStyleOptionFrame, QSystemTrayIcon, QApplication, QStyleFactory
from PyQt6.QtWidgets import QMessageBox, QProgressBar, QSplitter

from application.notification import IObserver, NotificationCenter
from application.python import Null, limit
from zope.interface import implementer

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.datatypes import Path
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.session import IllegalStateError

from blink.aboutpanel import AboutPanel
from blink.accounts import AccountModel, ActiveAccountModel, ServerToolsAccountModel, ServerToolsWindow
from blink.contacts import Contact, ContactEditorDialog, ContactModel, ContactSearchModel, URIUtils, ContactURI, conversation_uri
from blink.filetransferwindow import FileTransferWindow
from blink.history import HistoryManager, conversation_key
from blink.messages import MessageManager
from blink.launcher import LauncherBadge
from blink.logging import ActivityLog
from blink.messagepane import MessagePane
from blink.preferences import PreferencesWindow
from blink.sessions import ConferenceDialog, SessionManager, AudioSessionModel, StreamDescription
from blink.configuration.datatypes import IconDescriptor, FileURL, PresenceState
from blink.configuration.settings import BlinkSettings
from blink.presence import PendingWatcherDialog
from blink.resources import ApplicationData, IconManager, Resources, themed_icon
from blink.util import run_in_gui_thread, translate
from blink.widgets.buttons import AccountState, SwitchViewButton
from blink.widgets.color import follow_theme


__all__ = ['MainWindow']


ui_class, base_class = uic.loadUiType(Resources.get('blink.ui'))


@implementer(IObserver)
class MainWindow(base_class, ui_class):

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
        notification_center.add_observer(self, name='BlinkSessionTransferNewOutgoing')
        notification_center.add_observer(self, name='BlinkFileTransferNewIncoming')
        notification_center.add_observer(self, name='BlinkFileTransferNewOutgoing')
        notification_center.add_observer(self, name='BlinkUnreadMessagesChanged')
        self.launcher_badge = LauncherBadge()       # the unread count on the dock icon
        # an incoming message changes the unread counts: recounted from history, coalesced
        self._unread_timer = QTimer(self)
        self._unread_timer.setSingleShot(True)
        self._unread_timer.setInterval(300)
        self._unread_timer.timeout.connect(lambda: HistoryManager().message_history.get_unread_messages())
        notification_center.add_observer(self, name='ChatSessionUnreadMessagesCountChanged')
        notification_center.add_observer(self, name='BlinkMessageHistoryUnreadMessagesDidLoad')
        notification_center.add_observer(self, name='BlinkMessageNewUnread')
        notification_center.add_observer(self, name='BlinkMessageHistoryMessageDidStore')
        notification_center.add_observer(self, name='BlinkSessionConfirmReadMessages')
        notification_center.add_observer(self, name='BlinkConfirmReadMessagesOnOtherDevice')
        notification_center.add_observer(self, name='BlinkMessagePaneDidReadConversation')
        notification_center.add_observer(self, name='BlinkJournalProgress')

        notification_center.add_observer(self, sender=AccountManager())

        icon_manager = IconManager()

        self.pending_watcher_dialogs = []
        self.unread_messages = {}

        self.mwi_icons = [QIcon(Resources.get('icons/mwi-%d.png' % i)) for i in range(0, 11)]
        self.mwi_icons.append(QIcon(Resources.get('icons/mwi-many.png')))

        with Resources.directory:
            self.setupUi()

        self.setWindowTitle('Blink')

        geometry = QSettings().value("main_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        self.default_icon_path = Resources.get('icons/default-avatar.png')
        self.default_icon = themed_icon(self.default_icon_path)
        self._set_button_icons()
        follow_theme(self)
        self.last_icon_directory = Path('~').normalized
        self.set_user_icon(icon_manager.get('avatar'))
        self.enable_call_buttons(False)
        self.conference_button.setEnabled(False)
        self.hangup_all_button.setEnabled(False)
        self.sip_server_settings_action.setEnabled(False)
        self.search_for_people_action.setEnabled(False)
        self.history_on_server_action.setEnabled(False)
        self.main_view.setCurrentWidget(self.contacts_panel)
        self.contacts_view.setCurrentWidget(self.contact_list_panel)
        self.search_view.setCurrentWidget(self.search_list_panel)
        self.export_pgp_key_action.setEnabled(False)
        self.open_unread_messages_button.setEnabled(False)
        self.open_unread_messages_button.setVisible(False)
        self.active_sessions_label.hide()
        self.switch_view_button.hide()      # shown only while there are calls (_SH_AudioSessionModelChangedStructure)

        # System tray
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.system_tray_icon = QSystemTrayIcon(QIcon(Resources.get('icons/blink.png')), self)
            self.system_tray_icon.activated.connect(self._SH_SystemTrayIconActivated)
            menu = QMenu(self)
            menu.addAction(translate("main_window", "Show"), self._AH_SystemTrayShowWindow)
            menu.addAction(QIcon(Resources.get('icons/application-exit.png')), translate("main_window", "Quit"), self._AH_QuitActionTriggered)
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

        # History
        self.history_manager = HistoryManager()

        # Windows, dialogs and panels
        self.about_panel = AboutPanel(self)
        self.conference_dialog = ConferenceDialog(self)
        self.contact_editor_dialog = ContactEditorDialog(self)
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
        self.video_call_button.clicked.connect(self._SH_VideoCallButtonClicked)
        self.chat_session_button.clicked.connect(self._SH_ChatSessionButtonClicked)
        self.back_to_contacts_button.clicked.connect(self.search_box.clear)  # this can be set in designer -Dan
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
        self.open_unread_messages_button.clicked.connect(self._AH_ShowUnreadMessagesActionTriggered)

        # Blink menu actions
        self.about_action.triggered.connect(self.about_panel.show)
        self.add_account_action.triggered.connect(self.preferences_window.show_add_account_dialog)
        self.manage_accounts_action.triggered.connect(self.preferences_window.show_for_accounts)
        self.help_action.triggered.connect(partial(QDesktopServices.openUrl, QUrl('https://icanblink.com/help/manual-qt/')))
        self.preferences_action.triggered.connect(self.preferences_window.show)
        self.received_messages_sound_action.triggered.connect(self._AH_ReceivedMessagesSoundActionTriggered)
        self.answering_machine_action.triggered.connect(self._AH_EnableAnsweringMachineActionTriggered)
        self.release_notes_action.triggered.connect(partial(QDesktopServices.openUrl, QUrl('https://icanblink.com/changelog-linux/')))
        self.quit_action.triggered.connect(self._AH_QuitActionTriggered)

        # Call menu actions
        self.redial_action.triggered.connect(self._AH_RedialActionTriggered)
        self.join_conference_action.triggered.connect(self.conference_dialog.show)
        self.history_menu.aboutToShow.connect(self._SH_HistoryMenuAboutToShow)
        self.history_menu.triggered.connect(self._AH_HistoryMenuTriggered)
        self.transfer_menu.aboutToShow.connect(self._SH_TransferMenuAboutToShow)
        self.transfer_menu.triggered.connect(self._AH_TransferMenuTriggered)
        self.output_devices_group.triggered.connect(self._AH_AudioOutputDeviceChanged)
        self.input_devices_group.triggered.connect(self._AH_AudioInputDeviceChanged)
        self.alert_devices_group.triggered.connect(self._AH_AudioAlertDeviceChanged)
        self.video_devices_group.triggered.connect(self._AH_VideoDeviceChanged)
        self.mute_action.triggered.connect(self._SH_MuteButtonClicked)
        self.silent_action.triggered.connect(self._SH_SilentButtonClicked)
        self.auto_answer_action.triggered.connect(self._SH_AutoAnswerButtonClicked)
        self.auto_record_action.triggered.connect(self._SH_AutoRecordButtonClicked)

        # Tools menu actions
        self.sip_server_settings_action.triggered.connect(self._AH_SIPServerSettings)
        self.search_for_people_action.triggered.connect(self._AH_SearchForPeople)
        self.history_on_server_action.triggered.connect(self._AH_HistoryOnServer)
        self.google_contacts_action.triggered.connect(self._AH_GoogleContactsActionTriggered)

        self.export_pgp_key_action.triggered.connect(self._AH_ExportPGPkeyActionTriggered)
        self.generate_pgp_key.triggered.connect(self._AH_GeneratePGPkeyActionTriggered)

        # Devices menu
        self.devices_menu.aboutToShow.connect(self.refresh_devices)

        # Window menu actions
        self.chat_window_action.triggered.connect(self._AH_ChatWindowActionTriggered)
        self.transfers_window_action.triggered.connect(self._AH_TransfersWindowActionTriggered)
        self.logs_window_action.triggered.connect(self._AH_LogsWindowActionTriggered)
        self.message_pane_action.toggled.connect(self.set_message_pane_visible)

    def refresh_devices(self):
        """Read the sound devices again; a chosen device that is gone falls back to the system default.
        Each side is checked against its own list: a microphone need not be a speaker too."""
        SIPApplication.engine._ua.refresh_sound_devices()
        settings = SIPSimpleSettings()
        inputs = set(SIPApplication.engine.input_devices) | {'system_default', None}
        outputs = set(SIPApplication.engine.output_devices) | {'system_default', None}
        changed = False
        for name, available in (('input_device', inputs), ('output_device', outputs), ('alert_device', outputs)):
            device = getattr(settings.audio, name)
            if device not in available:
                ActivityLog().info(f'[audio] {name.replace("_", " ").capitalize()} {device} is gone, using the system default')
                setattr(settings.audio, name, 'system_default')
                changed = True
        if changed:
            settings.save()
        self._update_combined_audio_devices()

    def setupUi(self):
        super(MainWindow, self).setupUi(self)

        self.search_box.shortcut = QShortcut(self.search_box)
        self.search_box.shortcut.setKey('Ctrl+F')

        self.output_devices_group = QActionGroup(self)
        self.input_devices_group = QActionGroup(self)
        self.alert_devices_group = QActionGroup(self)
        self.video_devices_group = QActionGroup(self)

        self.screen_sharing_button.addAction(QAction(translate('main_window', 'Request screen'), self.screen_sharing_button, triggered=self._AH_RequestScreenActionTriggered))
        self.screen_sharing_button.addAction(QAction(translate('main_window', 'Share my screen'), self.screen_sharing_button, triggered=self._AH_ShareMyScreenActionTriggered))

        # adjust search box height depending on theme as the value set in designer isn't suited for all themes
        search_box = self.search_box
        option = QStyleOptionFrame()
        search_box.initStyleOption(option)
        frame_width = search_box.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth, option, search_box)
        if frame_width < 4:
            search_box.setMinimumHeight(20 + 2 * frame_width)

        # adjust the combo boxes for themes with too much padding (like the default theme on Ubuntu 10.04)
        option = QStyleOptionComboBox()
        self.identity.initStyleOption(option)
        wide_padding = self.identity.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, self.identity).height() < 10
        self.identity.setStyleSheet("""QComboBox { padding: 0px 4px 0px 4px; }""" if wide_padding else "")

        # the message journal's download and import, under the account; shown only when it takes a while
        self.journal_progress = QProgressBar(self)
        self.journal_progress.setTextVisible(True)
        font = self.journal_progress.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 0.8)
        else:
            font.setPixelSize(max(8, round(font.pixelSize() * 0.8)))
        self.journal_progress.setFont(font)
        self.journal_progress.setFixedHeight(self.journal_progress.fontMetrics().height() + 4)
        self.journal_progress.hide()
        self.window_layout.insertWidget(self.window_layout.indexOf(self.identity_widget) + 1, self.journal_progress)
        self.journal_progress_timer = QTimer(self)
        self.journal_progress_timer.setSingleShot(True)
        self.journal_progress_timer.setInterval(1500)
        self.journal_progress_timer.timeout.connect(self.journal_progress.show)

        # the message pane, right of the contact list (which becomes the conversation switcher);
        # closed at start whatever it was when Blink quit, so starting never shows (and reads) a conversation
        self.contacts_column = self.takeCentralWidget()
        self.contacts_column.setMinimumWidth(self.contacts_column_minimum_width)
        self.message_pane = MessagePane()
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.main_splitter.setObjectName('main_splitter')
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self.contacts_column)
        self.main_splitter.addWidget(self.message_pane)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.splitterMoved.connect(self._SH_MainSplitterMoved)
        self.setCentralWidget(self.main_splitter)
        self.message_pane.hide()

        self.message_pane_action = QAction(translate('main_window', 'Messages Pane'), self)
        self.message_pane_action.setCheckable(True)
        self.message_pane_action.setShortcut('Ctrl+4')
        first_action = self.window_menu.actions()[0] if self.window_menu.actions() else None
        self.window_menu.insertAction(first_action, self.message_pane_action)
        self.window_menu.insertSeparator(first_action)
        self.addAction(self.message_pane_action)     # the shortcut works with the menu bar hidden too

    contacts_column_minimum_width = 274

    @property
    def message_pane_width(self):
        try:
            width = int(QSettings().value('main_window/message_pane_width', MessagePane.default_width))
        except (TypeError, ValueError):
            width = MessagePane.default_width
        return max(width, MessagePane.minimum_width)

    def _save_message_pane_width(self):
        if self.message_pane.isVisible():
            QSettings().setValue('main_window/message_pane_width', self.message_pane.width())

    def _SH_MainSplitterMoved(self, position, index):
        self._save_message_pane_width()

    def set_message_pane_visible(self, visible):
        """Open or close the message pane; the window grows or shrinks by its width, the contact list keeps its own."""
        if self.message_pane_action.isChecked() != visible:
            self.message_pane_action.setChecked(visible)     # comes back here through toggled
            return
        if visible == self.message_pane.isVisible():
            return
        resizable = not (self.isMaximized() or self.isFullScreen())
        list_width = self.contacts_column.width()
        handle = self.main_splitter.handleWidth()
        if visible:
            pane_width = self.message_pane_width
            self.message_pane.show()
            if resizable:
                self.resize(list_width + handle + pane_width, self.height())
                self._keep_on_screen()
            self.main_splitter.setSizes([list_width, pane_width])
            ActivityLog().info(f'[ui] Message pane opened ({pane_width} px wide' + (f', showing {self.message_pane.key})' if self.message_pane.key else ', no conversation selected)'))
        else:
            self._save_message_pane_width()
            self.message_pane.hide()
            if resizable:
                self.resize(list_width, self.height())
            ActivityLog().info('[ui] Message pane closed')

    def _keep_on_screen(self):
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        if frame.right() > available.right():
            self.move(max(available.left(), self.x() - (frame.right() - available.right())), self.y())

    def closeEvent(self, event):
        # the geometry is kept without the pane, which is closed at the next start
        self.set_message_pane_visible(False)
        QSettings().setValue("main_window/geometry", self.saveGeometry())
        super(MainWindow, self).closeEvent(event)
        self.about_panel.close()
        self.contact_editor_dialog.close()
        self.server_tools_window.close()
        for dialog in self.pending_watcher_dialogs[:]:
            dialog.close()

    def show(self):
        first_show = not getattr(self, '_shown_once', False)
        self._shown_once = True
        super(MainWindow, self).show()
        self.raise_()
        self.activateWindow()
        if first_show:
            # the search box is the first tab stop and would take the focus: start on the contact list (Ctrl+F searches)
            self.contact_list.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_user_icon(self, icon):
        self._user_icon = icon
        self.account_state.setIcon(icon or self.default_icon)

    def apply_theme(self):
        self.default_icon = themed_icon(self.default_icon_path)
        self.set_user_icon(getattr(self, '_user_icon', None))
        self._set_button_icons()

    def _set_button_icons(self):
        """The bottom bar's glyphs are dark: light copies under a dark theme."""
        for button, filename in ((self.add_contact_button, 'icons/plus18.svg'), (self.audio_call_button, 'icons/handset.png'),
                                 (self.video_call_button, 'icons/camera.png'), (self.chat_session_button, 'icons/chat.png'),
                                 (self.screen_sharing_button, 'icons/screen.png')):
            button.setIcon(themed_icon(Resources.get(filename), '#d0d0d0'))
        bell = themed_icon(Resources.get('icons/bell-on.png'), '#d0d0d0')
        bell.addFile(Resources.get('icons/bell-off.png'), QSize(), QIcon.Mode.Normal, QIcon.State.On)     # silent stays red
        self.silent_button.setIcon(bell)
        microphone = themed_icon(Resources.get('icons/mic-on.svg'), '#d0d0d0')
        microphone.addFile(Resources.get('icons/mic-off.svg'), QSize(), QIcon.Mode.Normal, QIcon.State.On)
        self.mute_button.setIcon(microphone)

    def enable_call_buttons(self, enabled):  # todo: review this
        self.audio_call_button.setEnabled(enabled)
        self.video_call_button.setEnabled(enabled)
        self.chat_session_button.setEnabled(enabled)
        self.screen_sharing_button.setEnabled(enabled)

    def load_audio_devices(self):
        settings = SIPSimpleSettings()

        action_map = {}

        action = action_map['system_default'] = self.output_device_menu.addAction(translate('main_window', 'System default'))
        action.setData('system_default')
        action.setCheckable(True)
        self.output_devices_group.addAction(action)

        self.output_device_menu.addSeparator()

        for device in SIPApplication.engine.output_devices:
            action = action_map[device] = self.output_device_menu.addAction(device)
            action.setData(device)
            action.setCheckable(True)
            self.output_devices_group.addAction(action)

        action = action_map[None] = self.output_device_menu.addAction(translate('main_window', 'None'))
        action.setData(None)
        action.setCheckable(True)
        self.output_devices_group.addAction(action)

        active_action = action_map.get(settings.audio.output_device, Null)
        active_action.setChecked(True)

        action_map = {}

        action = action_map['system_default'] = self.input_device_menu.addAction(translate('main_window', 'System default'))
        action.setData('system_default')
        action.setCheckable(True)
        self.input_devices_group.addAction(action)

        self.input_device_menu.addSeparator()

        for device in SIPApplication.engine.input_devices:
            action = action_map[device] = self.input_device_menu.addAction(device)
            action.setData(device)
            action.setCheckable(True)
            self.input_devices_group.addAction(action)

        action = action_map[None] = self.input_device_menu.addAction(translate('main_window', 'None'))
        action.setData(None)
        action.setCheckable(True)
        self.input_devices_group.addAction(action)

        active_action = action_map.get(settings.audio.input_device, Null)
        active_action.setChecked(True)

        action_map = {}

        action = action_map['system_default'] = self.alert_device_menu.addAction(translate('main_window', 'System default'))
        action.setData('system_default')
        action.setCheckable(True)
        self.alert_devices_group.addAction(action)

        self.alert_device_menu.addSeparator()

        for device in SIPApplication.engine.output_devices:
            action = action_map[device] = self.alert_device_menu.addAction(device)
            action.setData(device)
            action.setCheckable(True)
            self.alert_devices_group.addAction(action)

        action = action_map[None] = self.alert_device_menu.addAction(translate('main_window', 'None'))
        action.setData(None)
        action.setCheckable(True)
        self.alert_devices_group.addAction(action)

        active_action = action_map.get(settings.audio.alert_device, Null)
        active_action.setChecked(True)

        self._load_combined_audio_devices()

    # generic ALSA/PulseAudio/PipeWire names: present on both sides on every machine, not a device you plug in
    combined_device_exclusions = {'system_default', 'default', 'pulse', 'pipewire', 'sysdefault', 'dmix', 'dsnoop', 'jack', 'oss', 'speex', 'upmix', 'vdownmix', 'samplerate', 'speexrate', 'lavrate'}

    def _load_combined_audio_devices(self):
        """At the top of the Devices menu, as on macOS: the devices that are both an input and an output
        (a USB headset, a speakerphone), each one item that selects it for both."""
        for action in getattr(self, 'combined_device_actions', []):
            self.devices_menu.removeAction(action)
            action.deleteLater()
        self.combined_device_actions = []
        outputs = list(SIPApplication.engine.output_devices)
        inputs = set(SIPApplication.engine.input_devices)
        devices = [device for device in outputs if device in inputs and device.split(':', 1)[0].strip().lower() not in self.combined_device_exclusions]
        if not devices:
            return
        first = self.devices_menu.actions()[0] if self.devices_menu.actions() else None
        short_names = [device.split(':', 1)[0].strip() or device for device in devices]
        for device, short in zip(devices, short_names):
            label = short if short_names.count(short) == 1 else device
            action = QAction(label, self.devices_menu)
            action.setCheckable(True)
            action.setToolTip(device)
            action.setData(device)
            action.triggered.connect(partial(self._AH_CombinedAudioDeviceTriggered, device))
            self.devices_menu.insertAction(first, action)
            self.combined_device_actions.append(action)
        separator = QAction(self.devices_menu)
        separator.setSeparator(True)
        self.devices_menu.insertAction(first, separator)
        self.combined_device_actions.append(separator)
        self.devices_menu.setToolTipsVisible(True)
        self._update_combined_audio_devices()

    def _update_combined_audio_devices(self):
        settings = SIPSimpleSettings()
        for action in getattr(self, 'combined_device_actions', []):
            if not action.isSeparator():
                action.setChecked(settings.audio.input_device == action.data() == settings.audio.output_device)

    def _AH_CombinedAudioDeviceTriggered(self, device, checked=False):
        settings = SIPSimpleSettings()
        settings.audio.input_device = device
        settings.audio.output_device = device
        settings.save()
        ActivityLog().info(f'[audio] {device} selected for both input and output')
        self._update_combined_audio_devices()

    def load_video_devices(self):
        settings = SIPSimpleSettings()

        action_map = {}

        action = action_map['system_default'] = self.video_camera_menu.addAction(translate('main_window', 'System default'))
        action.setData('system_default')
        action.setCheckable(True)
        self.video_devices_group.addAction(action)

        self.video_camera_menu.addSeparator()

        for device in SIPApplication.engine.video_devices:
            action = action_map[device] = self.video_camera_menu.addAction(device)
            action.setData(device)
            action.setCheckable(True)
            self.video_devices_group.addAction(action)

        action = action_map[None] = self.video_camera_menu.addAction(translate('main_window', 'None'))
        action.setData(None)
        action.setCheckable(True)
        self.video_devices_group.addAction(action)

        active_action = action_map.get(settings.video.device, Null)
        active_action.setChecked(True)

    def _AH_AccountActionTriggered(self, enabled):
        account = self.sender().data()
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

    def _AH_VideoDeviceChanged(self, action):
        settings = SIPSimpleSettings()
        settings.video.device = action.data()
        settings.save()

    def _AH_ReceivedMessagesSoundActionTriggered(self, checked):
        settings = SIPSimpleSettings()
        settings.sounds.play_message_alerts = checked
        settings.save()

    def _AH_EnableAnsweringMachineActionTriggered(self, checked):
        settings = SIPSimpleSettings()
        settings.answering_machine.enabled = checked
        settings.save()

    def _AH_GoogleContactsActionTriggered(self):
        settings = SIPSimpleSettings()
        settings.google_contacts.enabled = not settings.google_contacts.enabled
        settings.save()

    def _AH_RedialActionTriggered(self):
        session_manager = SessionManager()
        if session_manager.last_dialed_uri is not None:
            contact, contact_uri = URIUtils.find_contact(session_manager.last_dialed_uri)
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio')])  # TODO: remember used media types and redial with them. -Saul

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

    def _AH_ChatWindowActionTriggered(self, checked):
        """Window > MSRP sessions: the window for MSRP chat, video and screen sharing, even if it has none."""
        blink = QApplication.instance()
        blink.chat_window.show()

    def _AH_ShowUnreadMessagesActionTriggered(self, checked):
        """The conversation with the newest unread message, in the message pane."""
        keys = [key for key, count in self.unread_messages.items() if count]
        if not keys:
            return
        from blink.history import ConversationPreviews
        times = ConversationPreviews().message_times
        key = max(keys, key=lambda key: (times.get(key) is not None, times.get(key) or 0))
        contact = self._select_conversation_row(key)
        if contact is None:
            contact, contact_uri = URIUtils.find_contact(key)
            self.show_conversation_in_pane(contact, contact_uri)
        else:
            self.set_message_pane_visible(True)
            self._bring_to_front()
        ActivityLog().info(f'[Message with {key}] Opened from the new message bar ({len(keys)} conversations with unread messages)')

    def _select_conversation_row(self, key):
        """Select the contact list row of a conversation (in the Messages group if it is there) and return its contact, or None."""
        model = self.contact_model
        rows = [(position, item) for position, item in enumerate(model.items) if isinstance(item, Contact) and key in item.conversation_keys]
        if not rows:
            return None
        from blink.contacts import is_messages_group
        position, contact = next(((position, item) for position, item in rows if is_messages_group(getattr(item.group, 'settings', None))), rows[0])
        if self.search_box.text():
            self.search_box.clear()
        self.main_view.setCurrentWidget(self.contacts_panel)
        self.contacts_view.setCurrentWidget(self.contact_list_panel)
        index = model.index(position)
        if self.contact_list.isRowHidden(position) and getattr(contact.group, 'widget', None) is not None:
            contact.group.widget.collapse_button.setChecked(False)     # expand its group
        self.contact_list.selectionModel().select(index, self.contact_list.selectionModel().SelectionFlag.ClearAndSelect)
        self.contact_list.setCurrentIndex(index)
        self.contact_list.scrollTo(index)
        return contact

    def show_conversation_in_pane(self, contact, contact_uri):
        """Open the message pane on the conversation with a contact, on this address."""
        from blink.history import conversation_key
        from blink.contacts import neighbour_instance_id
        key = neighbour_instance_id(contact, str(contact_uri.uri)) or conversation_key(str(contact_uri.uri), AccountManager().default_account)
        self.set_message_pane_visible(True)
        self.message_pane.show_conversation(contact, contact_uri, key)
        self._bring_to_front()

    def _bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _AH_ExportPGPkeyActionTriggered(self, checked):
        account = self.identity.itemData(self.identity.currentIndex()).account
        account = account if account is not BonjourAccount() else None
        MessageManager().export_private_key(account)

    def _AH_GeneratePGPkeyActionTriggered(self, checked):
        account = self.identity.itemData(self.identity.currentIndex()).account
        account = account if account is not BonjourAccount() else None
        title = translate('main_window', "Generate new private key")
        message = translate('main_window', "You should generate a new private key for %s only if one of your devices have been compromised. Do you want to generate a new private key?") % (account.id)
        if QMessageBox.critical(self, title, message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            MessageManager().generate_private_key(account)

    def _AH_TransfersWindowActionTriggered(self, checked):
        self.filetransfer_window.show()

    def _AH_LogsWindowActionTriggered(self, checked):
        blink = QApplication.instance()
        blink.logs_window.show()

    def _AH_VoicemailActionTriggered(self, checked):
        account = self.sender().data()
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
            action = self.history_menu.addAction(translate("main_window", "Call history is empty"))
            action.setEnabled(False)

    def _SH_TransferMenuAboutToShow(self):
        self.transfer_menu.clear()

        session_manager = SessionManager()
        if not session_manager.active_session:
            action = self.transfer_menu.addAction(translate("main_window", "No active call"))
            action.setEnabled(False)
            return

        for session in self.session_model.sessions:
            if session.active:
                continue

            title = session.blink_session.contact.name
            action = self.transfer_menu.addAction(translate("main_window", "To call with %s" % title))
            action.session = session.blink_session
            action.remote_uri = session.blink_session.contact_uri

        search_text = self.search_box.text()
        if len(search_text):
            selected_items = self.search_list.selectionModel().selectedIndexes()
            if selected_items:
                item = selected_items[0].data(Qt.ItemDataRole.UserRole) if selected_items else None
                if isinstance(item, Contact):
                    selected_uri = item.uri
                    title = item.name 
                    action = self.transfer_menu.addAction(translate("main_window", "To found contact %s" % title))
                    action.session = None
                    action.remote_uri = item.uri
            else:
                contact, contact_uri = URIUtils.find_contact(search_text)
                if contact_uri:
                    title = contact.name
                    action = self.transfer_menu.addAction(translate("main_window", "To search contact %s" % title))
                    action.session = None
                    action.remote_uri = contact_uri
            return

        try:
            selected_items = self.contact_list.selectionModel().selectedIndexes()
        except IndexError:
            pass
        else:
            item = selected_items[0].data(Qt.ItemDataRole.UserRole) if selected_items else None
            if isinstance(item, Contact):
                if item.uri == session_manager.active_session.contact_uri:
                    return
                title = item.name 
                action = self.transfer_menu.addAction(translate("main_window", "To selected contact %s" % title))
                action.session = None
                action.remote_uri = item.uri

    def _AH_TransferMenuTriggered(self, action):
        session_manager = SessionManager()
        if not session_manager.active_session:
            return

        try:
            session_manager.active_session.transfer(action.remote_uri, replaced_session=action.session)
        except IllegalStateError:
            pass
        else:
            self.main_view.setCurrentWidget(self.sessions_panel)

    def _AH_HistoryMenuTriggered(self, action):
        account_manager = AccountManager()
        session_manager = SessionManager()
        try:
            account = account_manager.get_account(action.entry.account_id)
        except KeyError:
            account = None
        contact, contact_uri = URIUtils.find_contact(action.entry.uri)
        session_manager.create_session(contact, contact_uri, [StreamDescription('audio')], account=account)  # TODO: memorize media type and use it? -Saul (not sure about history in/out -Dan)

    def _AH_SystemTrayShowWindow(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _AH_QuitActionTriggered(self):
        if self.system_tray_icon is not None:
            self.system_tray_icon.hide()
        QApplication.instance().quit()

    def _SH_AccountStateChanged(self):
        self.activity_note.setText(self.account_state.note)
        if self.account_state.state is AccountState.Invisible:
            self.activity_note.inactiveText = translate('main_window', '(invisible)')
            self.activity_note.setEnabled(False)
        else:
            if not self.activity_note.isEnabled():
                self.activity_note.inactiveText = translate('main_window', 'Add an activity note here')
                self.activity_note.setEnabled(True)
        if not self.account_state.state.internal:
            self.saved_account_state = None
        blink_settings = BlinkSettings()
        blink_settings.presence.current_state = PresenceState(self.account_state.state, self.account_state.note)
        blink_settings.presence.state_history = [PresenceState(state, note) for state, note in self.account_state.history]
        blink_settings.save()

    def _SH_AccountStateClicked(self, checked):
        filename = QFileDialog.getOpenFileName(self, translate('main_window', 'Select Icon'), self.last_icon_directory, "Images (*.png *.tiff *.jpg *.xmp *.svg)")[0]
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
                contact = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
                contact_uri = contact.uri
            else:
                contact, contact_uri = URIUtils.find_contact(self.search_box.text())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio')])

    def _SH_VideoCallButtonClicked(self):
        list_view = self.contact_list if self.contacts_view.currentWidget() is self.contact_list_panel else self.search_list
        if list_view.detail_view.isVisible():
            list_view.detail_view._AH_StartVideoCall()
        else:
            selected_indexes = list_view.selectionModel().selectedIndexes()
            if selected_indexes:
                contact = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
                contact_uri = contact.uri
            else:
                contact, contact_uri = URIUtils.find_contact(self.search_box.text())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('audio'), StreamDescription('video')])

    def _SH_ChatSessionButtonClicked(self):
        list_view = self.contact_list if self.contacts_view.currentWidget() is self.contact_list_panel else self.search_list
        if list_view.detail_view.isVisible():
            list_view.detail_view._AH_StartChatSession()
        else:
            selected_indexes = list_view.selectionModel().selectedIndexes()
            if selected_indexes:
                contact = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
                contact_uri = contact.uri
            else:
                contact, contact_uri = URIUtils.find_contact(self.search_box.text())
            session_manager = MessageManager()
            session_manager.create_message_session(contact_uri.uri, contact.name, selected=True)

    def _AH_RequestScreenActionTriggered(self):
        list_view = self.contact_list if self.contacts_view.currentWidget() is self.contact_list_panel else self.search_list
        if list_view.detail_view.isVisible():
            list_view.detail_view._AH_RequestScreen()
        else:
            selected_indexes = list_view.selectionModel().selectedIndexes()
            if selected_indexes:
                contact = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
                contact_uri = contact.uri
            else:
                contact, contact_uri = URIUtils.find_contact(self.search_box.text())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('screen-sharing', mode='viewer'), StreamDescription('audio')])

    def _AH_ShareMyScreenActionTriggered(self):
        list_view = self.contact_list if self.contacts_view.currentWidget() is self.contact_list_panel else self.search_list
        if list_view.detail_view.isVisible():
            list_view.detail_view._AH_ShareMyScreen()
        else:
            selected_indexes = list_view.selectionModel().selectedIndexes()
            if selected_indexes:
                contact = selected_indexes[0].data(Qt.ItemDataRole.UserRole)
                contact_uri = contact.uri
            else:
                contact, contact_uri = URIUtils.find_contact(self.search_box.text())
            session_manager = SessionManager()
            session_manager.create_session(contact, contact_uri, [StreamDescription('screen-sharing', mode='server'), StreamDescription('audio')])

    def _SH_BreakConference(self):
        active_session = self.session_list.selectionModel().selectedIndexes()[0].data(Qt.ItemDataRole.UserRole)
        self.session_model.breakConference(active_session.client_conference)

    def _SH_ContactListSelectionChanged(self, selected, deselected):
        account_manager = AccountManager()
        selected_items = self.contact_list.selectionModel().selectedIndexes()
        self.enable_call_buttons(account_manager.default_account is not None and len(selected_items) == 1 and isinstance(selected_items[0].data(Qt.ItemDataRole.UserRole), Contact))
        self._follow_selection(selected_items)

    def _follow_selection(self, selected_indexes):
        """The message pane shows the one selected contact's conversation (and opens only when asked)."""
        contact = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if len(selected_indexes) == 1 else None
        if isinstance(contact, Contact):
            uri, key = conversation_uri(contact)
            if uri is not None:
                self.message_pane.show_conversation(contact, uri, key)
                return
        self.message_pane.clear()

    def _SH_ContactModelAddedItems(self, items):
        if not self.search_box.text():
            return
        active_widget = self.search_list_panel if self.contact_search_model.rowCount() else self.not_found_panel
        self.search_view.setCurrentWidget(active_widget)

    def _SH_ContactModelRemovedItems(self, items):
        if not self.search_box.text():
            return
        if any(type(item) is Contact for item in items) and self.contact_search_model.rowCount() == 0:
            self.search_box.clear()  # check this. it is no longer be the correct behaviour as now contacts can be deleted from remote -Dan
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
        account = account_manager.default_account
        if account is BonjourAccount() or not account.sms.enable_pgp or account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized):
            self.export_pgp_key_action.setEnabled(False)
        else:
            self.export_pgp_key_action.setEnabled(True)

    def _SH_IdentityCurrentIndexChanged(self, index):
        if index != -1:
            account = self.identity.itemData(index).account
            self.display_name.setText(account.display_name or '')
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
            self.enable_call_buttons(account_manager.default_account is not None and len(selected_items) <= 1)
        else:
            self.contacts_view.setCurrentWidget(self.contact_list_panel)
            selected_items = self.contact_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(account_manager.default_account is not None and len(selected_items) == 1 and type(selected_items[0].data(Qt.ItemDataRole.UserRole)) is Contact)
        self.search_list.detail_model.contact = None
        self.search_list.detail_view.hide()

    def _SH_SearchListSelectionChanged(self, selected, deselected):
        account_manager = AccountManager()
        selected_items = self.search_list.selectionModel().selectedIndexes()
        self.enable_call_buttons(account_manager.default_account is not None and len(selected_items) <= 1)
        self._follow_selection(selected_items)

    def _SH_ServerToolsAccountModelChanged(self, parent_index, start, end):
        server_tools_enabled = self.server_tools_account_model.rowCount() > 0
        self.sip_server_settings_action.setEnabled(server_tools_enabled)
        self.search_for_people_action.setEnabled(server_tools_enabled)
        self.history_on_server_action.setEnabled(server_tools_enabled)

    def _SH_SessionListSelectionChanged(self, selected, deselected):
        selected_indexes = selected.indexes()
        active_session = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else Null
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

    @property
    def total_unread_messages(self):
        new_messages = 0
        for k in self.unread_messages.copy().keys():
            new_messages = new_messages + self.unread_messages[k]
        return new_messages

    @run_in_gui_thread
    def _NH_BlinkConfirmReadMessagesOnOtherDevice(self, notification):
        try:
            del(self.unread_messages[notification.data.remote_uri])
        except KeyError:
            pass
        else:
            NotificationCenter().post_notification('BlinkUnreadMessagesChanged')

    @run_in_gui_thread
    def _NH_BlinkMessagePaneDidReadConversation(self, notification):
        if self.unread_messages.pop(notification.data.remote_uri, None) is not None:
            NotificationCenter().post_notification('BlinkUnreadMessagesChanged')

    def changeEvent(self, event):
        super(MainWindow, self).changeEvent(event)
        if event.type() in (event.Type.ActivationChange, event.Type.WindowStateChange) and hasattr(self, 'message_pane'):
            self.message_pane.check_read()      # becoming active (or restored) may make the shown conversation read

    @run_in_gui_thread
    def _NH_BlinkSessionConfirmReadMessages(self, notification):
        session = notification.sender
        # history keys a Bonjour neighbour by its instance id, everyone else by user@host
        keys = {str(session.uri).partition(':')[2], str(session.contact_uri.uri).partition(';')[0],
                conversation_key(session.contact_uri.uri, session.account), conversation_key(session.uri, session.account)}
        if session.remote_instance_id:
            keys.add(session.remote_instance_id)
        removed = [self.unread_messages.pop(key) for key in keys if key in self.unread_messages]
        if removed:
            NotificationCenter().post_notification('BlinkUnreadMessagesChanged')

    @run_in_gui_thread
    def _NH_BlinkUnreadMessagesChanged(self, notification):
        # the count is on the Messages group of the contact list (ContactModel) and on the dock icon,
        # not above the call buttons
        self.open_unread_messages_button.setVisible(False)
        self.launcher_badge.set_count(self.total_unread_messages)
        self._update_tray_icon(self.total_unread_messages)

    _tray_count = 0

    def _update_tray_icon(self, count):
        """The unread count on the icon in the system tray (the top bar): a red badge with the number."""
        if self.system_tray_icon is None or count == self._tray_count:
            return
        self._tray_count = count
        icon = QIcon(Resources.get('icons/blink.png'))
        if count:
            size = 64
            pixmap = icon.pixmap(size, size)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            text = str(count) if count < 100 else '99+'
            font = painter.font()
            font.setBold(True)
            font.setPixelSize(int(size * (0.42 if len(text) < 3 else 0.32)))
            painter.setFont(font)
            diameter = int(size * 0.62)
            width = max(diameter, painter.fontMetrics().horizontalAdvance(text) + int(size * 0.18))
            badge = QRectF(size - width, 0, width, diameter)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#e0245e'))
            painter.drawRoundedRect(badge, diameter / 2, diameter / 2)
            painter.setPen(QColor('#ffffff'))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
            painter.end()
            icon = QIcon(pixmap)
        self.system_tray_icon.setIcon(icon)
        self.system_tray_icon.setToolTip(translate('main_window', 'Blink: 1 unread message') if count == 1 else
                                         translate('main_window', 'Blink: %d unread messages') % count if count else 'Blink')

    def _NH_BlinkMessageNewUnread(self, notification):
        uri = conversation_key(notification.sender)     # keyed as history files the conversation

        try:
            self.unread_messages[uri]
        except KeyError:
            self.unread_messages[uri] = 1
        else:
            self.unread_messages[uri] = self.unread_messages[uri] + 1

        NotificationCenter().post_notification('BlinkUnreadMessagesChanged')

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        if getattr(notification.data, 'direction', None) == 'incoming':
            self._unread_timer.start()

    def _NH_BlinkMessageHistoryUnreadMessagesDidLoad(self, notification):
        unread_messages = notification.data.unread_messages
        total = 0
        self.unread_messages = {}
        for k in unread_messages.keys():
            try:
                um = self.unread_messages[k]
            except KeyError:
                self.unread_messages[k] = unread_messages[k]
            else:
                self.unread_messages[k] = self.unread_messages[k] + unread_messages[k]
            total = total + self.unread_messages[k]

        NotificationCenter().post_notification('BlinkUnreadMessagesChanged')

    def hide_new_messages_label(self):
        self.active_sessions_label.setVisible(False)

    def _SH_AudioSessionModelChangedStructure(self):
        active_sessions = self.session_model.active_sessions
        self.switch_view_button.active_calls = len(active_sessions)     # the red "Return to the call" button, no banner
        self.switch_view_button.setVisible(bool(self.session_model.sessions))
        self.hangup_all_button.setEnabled(any(active_sessions))
        selected_indexes = self.session_list.selectionModel().selectedIndexes()
        active_session = selected_indexes[0].data(Qt.ItemDataRole.UserRole) if selected_indexes else Null
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
                self.account_state.setState(AccountState.Busy.Internal, note=translate('main_window', 'On the phone'))
        elif self.saved_account_state is not None:
            state, note = self.saved_account_state
            self.saved_account_state = None
            self.account_state.setState(state, note)

    def _SH_AutoAnswerButtonClicked(self, answer):
        settings = SIPSimpleSettings()
        settings.sip.auto_answer = not settings.sip.auto_answer
        settings.save()

    def _SH_AutoRecordButtonClicked(self, answer):
        settings = SIPSimpleSettings()
        settings.sip.auto_record = not settings.sip.auto_record
        settings.save()

    def _SH_SilentButtonClicked(self, silent):
        settings = SIPSimpleSettings()
        settings.audio.silent = silent
        settings.save()

    def _SH_SwitchViewButtonChangedView(self, view):
        self.main_view.setCurrentWidget(self.contacts_panel if view is SwitchViewButton.ContactView else self.sessions_panel)

    def _SH_PendingWatcherDialogFinished(self, result):
        self.pending_watcher_dialogs.remove(self.sender())

    def _SH_SystemTrayIconActivated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show()
            self.raise_()
            self.activateWindow()

    def _NH_BlinkJournalProgress(self, notification):
        phase = notification.data.phase
        bar = self.journal_progress
        if phase == 'done':
            self.journal_progress_timer.stop()
            bar.hide()
            return
        if not bar.isVisible() and not self.journal_progress_timer.isActive():
            self.journal_progress_timer.start()
        account = notification.sender.id
        done, total = notification.data.done or 0, notification.data.total
        label = translate('main_window', 'Downloading messages') if phase == 'download' else translate('main_window', 'Importing messages')
        if total:
            bar.setRange(0, total)
            bar.setValue(min(done, total))
            bar.setFormat(f'{label} %v/%m (%p%)')
        else:
            # the total is not known (a server without X-Sylk-Journal-Remaining); not the busy mode
            # (range 0, 0): most styles draw no text in it, and the count is the point
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setFormat(f'{label} {done}')
        bar.setToolTip(f'{label} of {account}: {done} of {total}' if total else f'{label} of {account}: {done}')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        account_manager = AccountManager()
        settings = SIPSimpleSettings()
        self.silent_action.setChecked(settings.audio.silent)
        self.silent_button.setChecked(settings.audio.silent)
        self.auto_answer_action.setChecked(settings.sip.auto_answer)
        self.auto_record_action.setChecked(settings.sip.auto_record)
        self.answering_machine_action.setChecked(settings.answering_machine.enabled)
        self.received_messages_sound_action.setChecked(settings.sounds.play_message_alerts)
        if settings.google_contacts.enabled:
            self.google_contacts_action.setText(translate('main_window', 'Disable &Google Contacts'))
        else:
            self.google_contacts_action.setText(translate('main_window', 'Enable &Google Contacts...'))
        if not any(account.enabled for account in account_manager.iter_accounts()):
            self.display_name.setEnabled(False)
            self.activity_note.setEnabled(False)
            self.account_state.setEnabled(False)

    def _NH_SIPApplicationDidStart(self, notification):
        self.load_audio_devices()
        self.load_video_devices()
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification.center.add_observer(self, name='AudioDevicesDidChange')
        notification.center.add_observer(self, name='VideoDevicesDidChange')
        blink_settings = BlinkSettings()
        self.account_state.history = [(item.state, item.note) for item in blink_settings.presence.state_history]
        state = getattr(AccountState, blink_settings.presence.current_state.state, AccountState.Available)
        self.account_state.setState(state, blink_settings.presence.current_state.note)
        MessageManager()

    def _NH_AudioDevicesDidChange(self, notification):
        self.output_device_menu.clear()  # because actions are owned by the menu and only referenced by their corresponding action groups,
        self.input_device_menu.clear()   # clearing the menus will result in the actions automatically disappearing from the corresponding
        self.alert_device_menu.clear()   # action groups as well
        if self.session_model.active_sessions:
            added_devices = set(notification.data.new_devices).difference(notification.data.old_devices)
            if added_devices:
                new_device = added_devices.pop()
                settings = SIPSimpleSettings()
                settings.audio.input_device = new_device
                settings.audio.output_device = new_device
                settings.save()
        self.load_audio_devices()

    def _NH_VideoDevicesDidChange(self, notification):
        self.video_camera_menu.clear()  # actions will be removed automatically from the action group because they are owned by the menu and only referenced in the action group
        if self.session_model.active_sessions:
            added_devices = set(notification.data.new_devices).difference(notification.data.old_devices)
            if added_devices:
                settings = SIPSimpleSettings()
                settings.video.device = added_devices.pop()
                settings.save()
        self.load_video_devices()

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
            if 'audio.auto_answer' in notification.data.modified:
                self.auto_answer_action.setChecked(settings.audio.auto_answer)
            if 'audio.output_device' in notification.data.modified:
                action = next(action for action in self.output_devices_group.actions() if action.data() == settings.audio.output_device)
                action.setChecked(True)
            if 'audio.input_device' in notification.data.modified:
                action = next(action for action in self.input_devices_group.actions() if action.data() == settings.audio.input_device)
                action.setChecked(True)
            if 'audio.alert_device' in notification.data.modified:
                action = next(action for action in self.alert_devices_group.actions() if action.data() == settings.audio.alert_device)
                action.setChecked(True)
            if 'video.device' in notification.data.modified:
                action = next(action for action in self.video_devices_group.actions() if action.data() == settings.video.device)
                action.setChecked(True)
            if 'answering_machine.enabled' in notification.data.modified:
                self.answering_machine_action.setChecked(settings.answering_machine.enabled)
            if 'sounds.play_message_alerts' in notification.data.modified:
                self.received_messages_sound_action.setChecked(settings.sounds.play_message_alerts)
            if 'google_contacts.enabled' in notification.data.modified:
                if notification.sender.google_contacts.enabled:
                    self.google_contacts_action.setText(translate('main_window', 'Disable &Google Contacts'))
                else:
                    self.google_contacts_action.setText(translate('main_window', 'Enable &Google Contacts...'))
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
                action = next(action for action in self.accounts_menu.actions() if action.data() is account)
                action.setChecked(account.enabled)
            if 'display_name' in notification.data.modified and account is account_manager.default_account:
                self.display_name.setText(account.display_name or '')
            if {'enabled', 'message_summary.enabled', 'message_summary.voicemail_uri'}.intersection(notification.data.modified):
                action = next(action for action in self.voicemail_menu.actions() if action.data() is account)
                action.setVisible(False if account is BonjourAccount() else account.enabled)
                action.setEnabled(False if account is BonjourAccount() else account.voicemail_uri is not None)
            if 'sms.private_key' in notification.data.modified:
                if account is not BonjourAccount() and account.sms.enable_pgp and account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized):
                    self.export_pgp_key_action.setEnabled(True)


    # domains whose SIP servers present the certificate of another domain: the TLS name an
    # account of the domain gets when it has none (added, or loaded at start)
    tls_name_exceptions = {'sylk.link': 'sip2sip.info'}

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account

        if account is not BonjourAccount() and not account.sip.tls_name:
            tls_name = self.tls_name_exceptions.get(account.id.domain.lower())
            if tls_name:
                account.sip.tls_name = tls_name
                account.save()
                ActivityLog().info(f'[accounts] TLS name of {account.id} set to {tls_name}, the certificate of {account.id.domain} servers')

        action = self.accounts_menu.addAction(account.id if account is not BonjourAccount() else 'Bonjour')
        action.setEnabled(True if account is not BonjourAccount() else BonjourAccount.mdns_available)
        action.setCheckable(True)
        action.setChecked(account.enabled)
        action.setData(account)
        action.triggered.connect(self._AH_AccountActionTriggered)

        action = self.voicemail_menu.addAction(self.mwi_icons[0], account.id)
        action.setVisible(False if account is BonjourAccount() else account.enabled)
        action.setEnabled(False if account is BonjourAccount() else account.voicemail_uri is not None)
        action.setData(account)
        action.triggered.connect(self._AH_VoicemailActionTriggered)

    def _NH_SIPAccountManagerDidStart(self, notification):
        account = notification.sender.default_account
        if account is not BonjourAccount() and account.sms.enable_pgp and account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized):
            self.export_pgp_key_action.setEnabled(True)

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        account = notification.data.account
        action = next(action for action in self.accounts_menu.actions() if action.data() is account)
        self.accounts_menu.removeAction(action)
        action = next(action for action in self.voicemail_menu.actions() if action.data() is account)
        self.voicemail_menu.removeAction(action)

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        if notification.data.account is None:
            self.enable_call_buttons(False)
        else:
            selected_items = self.contact_list.selectionModel().selectedIndexes()
            self.enable_call_buttons(len(selected_items) == 1 and isinstance(selected_items[0].data(Qt.ItemDataRole.UserRole), Contact))

    def _NH_SIPAccountGotMessageSummary(self, notification):
        account = notification.sender
        summary = notification.data.message_summary
        action = next(action for action in self.voicemail_menu.actions() if action.data() is account)
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

    def _NH_BlinkSessionTransferNewOutgoing(self, notification):
        self.search_box.clear()
        self.switch_view_button.view = SwitchViewButton.SessionView

    def _NH_BlinkFileTransferNewIncoming(self, notification):
        self.filetransfer_window.show(activate=QApplication.activeWindow() is not None)

    def _NH_BlinkFileTransferNewOutgoing(self, notification):
        from blink.messagepane.uploads import is_http_upload
        if is_http_upload(notification.sender):
            return          # sent over HTTP: shown in the message pane, the window is for MSRP transfers
        self.filetransfer_window.show(activate=QApplication.activeWindow() is not None)


del ui_class, base_class
