# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['PreferencesWindow', 'AccountListView', 'SIPPortEditor']

import os
import urlparse

from PyQt4 import uic
from PyQt4.QtCore import Qt, QRegExp
from PyQt4.QtGui  import QActionGroup, QButtonGroup, QFileDialog, QListView, QListWidgetItem, QMessageBox, QRegExpValidator, QSpinBox, QStyle, QStyleOptionComboBox, QValidator

from application import log
from application.notification import IObserver, NotificationCenter
from application.python import Null, limit
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.errors import GNUTLSError
from zope.interface import implements

from sipsimple.account import Account, BonjourAccount, AccountManager
from sipsimple.application import SIPApplication
from sipsimple.configuration import DefaultValue
from sipsimple.configuration.datatypes import MSRPRelayAddress, PortRange, SIPProxyAddress
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from blink.accounts import AddAccountDialog
from blink.resources import ApplicationData, Resources
from blink.logging import LogManager
from blink.util import QSingleton, call_in_gui_thread, run_in_gui_thread



# LineEdit and ComboBox validators
#
class IDDPrefixValidator(QRegExpValidator):
    def __init__(self, parent=None):
        super(IDDPrefixValidator, self).__init__(QRegExp(u'[0-9+*#]+'), parent)

    def fixup(self, input):
        return super(IDDPrefixValidator, self).fixup(input or u'+')


class PrefixValidator(QRegExpValidator):
    def __init__(self, parent=None):
        super(PrefixValidator, self).__init__(QRegExp(u'(None|[0-9+*#]+)'), parent)

    def fixup(self, input):
        return super(PrefixValidator, self).fixup(input or u'None')


class HostnameValidator(QRegExpValidator):
    def __init__(self, parent=None):
        super(HostnameValidator, self).__init__(QRegExp(u'^([\w\-_]+(\.[\w\-_]+)*)?$', Qt.CaseInsensitive), parent)


class SIPAddressValidator(QRegExpValidator):
    def __init__(self, parent=None):
        super(SIPAddressValidator, self).__init__(QRegExp(u'^([\w\-_+%]+@[\w\-_]+(\.[\w\-_]+)*)?$', Qt.CaseInsensitive), parent)

    def fixup(self, input):
        if input and '@' not in input:
            preferences_window = self.parent()
            input += u'@%s' % preferences_window.selected_account.id.domain
        return super(SIPAddressValidator, self).fixup(input)


class WebURLValidator(QRegExpValidator):
    def __init__(self, parent=None):
        super(WebURLValidator, self).__init__(QRegExp(u'^(https?://[\w\-_]+(\.[\w\-_]+)*(:\d+)?(/.*)?)?$', Qt.CaseInsensitive), parent)


class XCAPRootValidator(WebURLValidator):
    def fixup(self, input):
        url = urlparse.urlparse(input)
        if not (url.scheme and url.netloc):
            input = u''
        return super(XCAPRootValidator, self).fixup(input)

    def validate(self, input, pos):
        state, input, pos = super(XCAPRootValidator, self).validate(input, pos)
        if state == QValidator.Acceptable:
            if input.endswith(('?', ';', '&')):
                state = QValidator.Invalid
            else:
                url = urlparse.urlparse(input)
                if url.params or url.query or url.fragment:
                    state = QValidator.Invalid
                elif url.port is not None:
                    port = int(url.port)
                    if not (0 < port <= 65535):
                        state = QValidator.Invalid
        return state, input, pos


# Custom widgets used in preferences.ui
#
class SIPPortEditor(QSpinBox):
    def __init__(self, parent=None):
        super(SIPPortEditor, self).__init__(parent)
        self.setRange(0, 65535)
        self.sibling = Null # if there is a sibling port, its value is invalid for this port

    def stepBy(self, steps):
        value = self.value()
        sibling_value = self.sibling.value()
        if value+steps == sibling_value != 0:
            steps += steps/abs(steps) # add one more unit in the right direction
        if 0 < value+steps < 1024:
            if steps < 0:
                steps = -value
            else:
                steps = 1024 - value
        if value+steps == sibling_value != 0:
            steps += steps/abs(steps) # add one more unit in the right direction
        return super(SIPPortEditor, self).stepBy(steps)

    def validate(self, input, pos):
        state, input, pos = super(SIPPortEditor, self).validate(input, pos)
        if state == QValidator.Acceptable:
            value = int(input)
            if 0 < value < 1024:
                state = QValidator.Intermediate
            elif value == self.sibling.value() != 0:
                state = QValidator.Intermediate
        return state, input, pos


class AccountListView(QListView):
    def __init__(self, parent=None):
        super(AccountListView, self).__init__(parent)
        #self.setItemDelegate(AccountDelegate(self))
        #self.setDropIndicatorShown(False)

    def setModel(self, model):
        selection_model = self.selectionModel() or Null
        selection_model.selectionChanged.disconnect(self._SH_SelectionModelSelectionChanged)
        super(AccountListView, self).setModel(model)
        self.selectionModel().selectionChanged.connect(self._SH_SelectionModelSelectionChanged)

    def _SH_SelectionModelSelectionChanged(self, selected, deselected):
        selection_model = self.selectionModel()
        selection = selection_model.selection()
        if selection_model.currentIndex() not in selection:
            index = selection.indexes()[0] if not selection.isEmpty() else self.model().index(-1)
            selection_model.setCurrentIndex(index, selection_model.Select)


ui_class, base_class = uic.loadUiType(Resources.get('preferences.ui'))

class PreferencesWindow(base_class, ui_class):
    __metaclass__ = QSingleton

    implements(IObserver)

    def __init__(self, account_model, parent=None):
        super(PreferencesWindow, self).__init__(parent)
        self.load_in_progress = False

        with Resources.directory:
            self.setupUi()

        self.setWindowTitle('Blink Preferences')
        self.setWindowIconText('Blink Preferences')

        self.account_list.setModel(account_model)
        self.delete_account_button.setEnabled(False)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')

        # Dialogs
        self.add_account_dialog = AddAccountDialog(self)

        # Signals
        self.toolbar.actionTriggered.connect(self._SH_ToolbarActionTriggered)

        # Account
        self.account_list.selectionModel().selectionChanged.connect(self._SH_AccountListSelectionChanged)
        self.add_account_button.clicked.connect(self.show_add_account_dialog)
        self.delete_account_button.clicked.connect(self._SH_DeleteAccountButtonClicked)

        # Account information
        self.account_enabled_button.clicked.connect(self._SH_AccountEnabledButtonClicked)
        self.display_name_editor.editingFinished.connect(self._SH_DisplayNameEditorEditingFinished)
        self.password_editor.editingFinished.connect(self._SH_PasswordEditorEditingFinished)

        # Account media settings
        self.account_audio_codecs_list.itemChanged.connect(self._SH_AccountAudioCodecsListItemChanged)
        try:
            self.account_audio_codecs_list.model().rowsMoved.connect(self._SH_AccountAudioCodecsListModelRowsMoved)
        except AttributeError:
            # Qt before 4.6 did not have the rowsMoved signal.
            # To be removed when we drop support for the older QT versions. -Dan
            self.account_audio_codecs_list.model().rowsInserted.connect(self._SH_AccountAudioCodecsListModelRowsInserted)
        self.reset_account_audio_codecs_button.clicked.connect(self._SH_ResetAudioCodecsButtonClicked)
        self.inband_dtmf_button.clicked.connect(self._SH_InbandDTMFButtonClicked)
        self.srtp_encryption_button.activated[str].connect(self._SH_SRTPEncryptionButtonActivated)

        # Account server settings
        self.always_use_my_proxy_button.clicked.connect(self._SH_AlwaysUseMyProxyButtonClicked)
        self.outbound_proxy_host_editor.editingFinished.connect(self._SH_OutboundProxyHostEditorEditingFinished)
        self.outbound_proxy_port.valueChanged[int].connect(self._SH_OutboundProxyPortValueChanged)
        self.outbound_proxy_transport_button.activated[str].connect(self._SH_OutboundProxyTransportButtonActivated)
        self.auth_username_editor.editingFinished.connect(self._SH_AuthUsernameEditorEditingFinished)
        self.always_use_my_msrp_relay_button.clicked.connect(self._SH_AlwaysUseMyMSRPRelayButtonClicked)
        self.msrp_relay_host_editor.editingFinished.connect(self._SH_MSRPRelayHostEditorEditingFinished)
        self.msrp_relay_port.valueChanged[int].connect(self._SH_MSRPRelayPortValueChanged)
        self.msrp_relay_transport_button.activated[str].connect(self._SH_MSRPRelayTransportButtonActivated)
        self.voicemail_uri_editor.editingFinished.connect(self._SH_VoicemailURIEditorEditingFinished)
        self.xcap_root_editor.editingFinished.connect(self._SH_XCAPRootEditorEditingFinished)
        self.server_tools_url_editor.editingFinished.connect(self._SH_ServerToolsURLEditorEditingFinished)
        self.conference_server_editor.editingFinished.connect(self._SH_ConferenceServerEditorEditingFinished)

        # Account network settings
        self.use_ice_button.clicked.connect(self._SH_UseICEButtonClicked)
        self.msrp_transport_button.activated[str].connect(self._SH_MSRPTransportButtonActivated)

        # Account advanced settings
        self.register_interval.valueChanged[int].connect(self._SH_RegisterIntervalValueChanged)
        self.publish_interval.valueChanged[int].connect(self._SH_PublishIntervalValueChanged)
        self.subscribe_interval.valueChanged[int].connect(self._SH_SubscribeIntervalValueChanged)
        self.reregister_button.clicked.connect(self._SH_ReregisterButtonClicked)
        self.idd_prefix_button.activated[str].connect(self._SH_IDDPrefixButtonActivated)
        self.prefix_button.activated[str].connect(self._SH_PrefixButtonActivated)
        self.account_tls_cert_file_editor.locationCleared.connect(self._SH_AccountTLSCertFileEditorLocationCleared)
        self.account_tls_cert_file_browse_button.clicked.connect(self._SH_AccountTLSCertFileBrowseButtonClicked)
        self.account_tls_verify_server_button.clicked.connect(self._SH_AccountTLSVerifyServerButtonClicked)

        # Audio devices
        self.audio_alert_device_button.activated[int].connect(self._SH_AudioAlertDeviceButtonActivated)
        self.audio_input_device_button.activated[int].connect(self._SH_AudioInputDeviceButtonActivated)
        self.audio_output_device_button.activated[int].connect(self._SH_AudioOutputDeviceButtonActivated)
        self.audio_sample_rate_button.activated[str].connect(self._SH_AudioSampleRateButtonActivated)
        self.enable_echo_cancelling_button.clicked.connect(self._SH_EnableEchoCancellingButtonClicked)

        # Audio codecs
        self.audio_codecs_list.itemChanged.connect(self._SH_AudioCodecsListItemChanged)
        try:
            self.audio_codecs_list.model().rowsMoved.connect(self._SH_AudioCodecsListModelRowsMoved)
        except AttributeError:
            # Qt before 4.6 did not have the rowsMoved signal.
            # To be removed when we drop support for the older QT versions. -Dan
            self.audio_codecs_list.model().rowsInserted.connect(self._SH_AudioCodecsListModelRowsInserted)

        # Answering machine
        self.enable_answering_machine_button.clicked.connect(self._SH_EnableAnsweringMachineButtonClicked)
        self.answer_delay.valueChanged[int].connect(self._SH_AnswerDelayValueChanged)
        self.max_recording.valueChanged[int].connect(self._SH_MaxRecordingValueChanged)

        # Chat and SMS
        self.auto_accept_chat_button.clicked.connect(self._SH_AutoAcceptChatButtonClicked)
        self.sms_replication_button.clicked.connect(self._SH_SMSReplicationButtonClicked)

        # File transfer
        self.auto_accept_files_button.clicked.connect(self._SH_AutoAcceptFilesButtonClicked)
        self.download_directory_editor.locationCleared.connect(self._SH_DownloadDirectoryEditorLocationCleared)
        self.download_directory_browse_button.clicked.connect(self._SH_DownloadDirectoryBrowseButtonClicked)

        # Alerts
        self.silence_alerts_button.clicked.connect(self._SH_SilenceAlertsButtonClicked)
        self.message_alerts_button.clicked.connect(self._SH_MessageAlertsButtonClicked)
        self.file_alerts_button.clicked.connect(self._SH_FileAlertsButtonClicked)

        # File logging
        self.trace_sip_button.clicked.connect(self._SH_TraceSIPButtonClicked)
        self.trace_msrp_button.clicked.connect(self._SH_TraceMSRPButtonClicked)
        self.trace_xcap_button.clicked.connect(self._SH_TraceXCAPButtonClicked)
        self.trace_notifications_button.clicked.connect(self._SH_TraceNotificationsButtonClicked)
        self.trace_pjsip_button.clicked.connect(self._SH_TracePJSIPButtonClicked)
        self.pjsip_trace_level.valueChanged[int].connect(self._SH_PJSIPTraceLevelValueChanged)
        self.clear_log_files_button.clicked.connect(self._SH_ClearLogFilesButtonClicked)

        # SIP and RTP
        self.sip_transports_button_group.buttonClicked.connect(self._SH_SIPTransportsButtonClicked)
        self.udp_port.valueChanged[int].connect(self._SH_UDPPortValueChanged)
        self.tcp_port.valueChanged[int].connect(self._SH_TCPPortValueChanged)
        self.tls_port.valueChanged[int].connect(self._SH_TLSPortValueChanged)
        self.media_ports_start.valueChanged[int].connect(self._SH_MediaPortsStartValueChanged)
        self.media_ports.valueChanged[int].connect(self._SH_MediaPortsValueChanged)
        self.session_timeout.valueChanged[int].connect(self._SH_SessionTimeoutValueChanged)
        self.rtp_timeout.valueChanged[int].connect(self._SH_RTPTimeoutValueChanged)

        # TLS
        self.tls_ca_file_editor.locationCleared.connect(self._SH_TLSCAFileEditorLocationCleared)
        self.tls_ca_file_browse_button.clicked.connect(self._SH_TLSCAFileBrowseButtonClicked)
        self.tls_timeout.valueChanged[float].connect(self._SH_TLSTimeoutValueChanged)

        # Setup initial state (show the acounts page right after start)
        self.accounts_action.trigger()
        self.account_tab_widget.setCurrentIndex(0)

    def setupUi(self):
        super(PreferencesWindow, self).setupUi(self)

        #self.rename_account_button.hide() # do not use this for the time being -Dan

        self.section_group = QActionGroup(self)
        self.section_group.setExclusive(True)
        for index, action in enumerate(action for action in self.toolbar.actions() if not action.isSeparator()):
            action.index = index
            self.section_group.addAction(action)

        for index in xrange(self.idd_prefix_button.count()):
            text = self.idd_prefix_button.itemText(index)
            self.idd_prefix_button.setItemData(index, None if text == "+" else text)
        for index in xrange(self.prefix_button.count()):
            text = self.prefix_button.itemText(index)
            self.prefix_button.setItemData(index, None if text == "None" else text)

        self.voicemail_uri_editor.setValidator(SIPAddressValidator(self))
        self.xcap_root_editor.setValidator(XCAPRootValidator(self))
        self.server_tools_url_editor.setValidator(WebURLValidator(self))
        self.conference_server_editor.setValidator(HostnameValidator(self))
        self.idd_prefix_button.setValidator(IDDPrefixValidator(self))
        self.prefix_button.setValidator(PrefixValidator(self))

        # Adding the button group in designer has issues on Ubuntu 10.04
        self.sip_transports_button_group = QButtonGroup(self)
        self.sip_transports_button_group.setObjectName("sip_transports_button_group")
        self.sip_transports_button_group.setExclusive(False)
        self.sip_transports_button_group.addButton(self.enable_udp_button)
        self.sip_transports_button_group.addButton(self.enable_tcp_button)
        self.sip_transports_button_group.addButton(self.enable_tls_button)

        self.enable_udp_button.name = 'udp'
        self.enable_tcp_button.name = 'tcp'
        self.enable_tls_button.name = 'tls'

        self.tcp_port.sibling = self.tls_port
        self.tls_port.sibling = self.tcp_port

        # Adjust some minimum label widths in order to better align settings in different group boxes, widgets or tabs

        # account server and network tab
        font_metrics = self.outbound_proxy_label.fontMetrics() # we assume all labels have the same font
        labels = (self.outbound_proxy_label, self.auth_username_label, self.msrp_relay_label,
                  self.voicemail_uri_label, self.xcap_root_label, self.server_tools_url_label,
                  self.conference_server_label, self.msrp_transport_label)
        text_width = max(font_metrics.width(label.text()) for label in labels) + 15
        self.outbound_proxy_label.setMinimumWidth(text_width)
        self.msrp_transport_label.setMinimumWidth(text_width)

        # account advanced tab
        font_metrics = self.register_interval_label.fontMetrics() # we assume all labels have the same font
        labels = (self.register_interval_label, self.publish_interval_label, self.subscribe_interval_label,
                  self.idd_prefix_label, self.prefix_label, self.account_tls_cert_file_label)
        text_width = max(font_metrics.width(label.text()) for label in labels) + 15
        self.register_interval_label.setMinimumWidth(text_width)
        self.idd_prefix_label.setMinimumWidth(text_width)
        self.account_tls_cert_file_label.setMinimumWidth(text_width)

        # audio settings
        font_metrics = self.answer_delay_label.fontMetrics() # we assume all labels have the same font
        labels = (self.audio_input_device_label, self.audio_output_device_label, self.audio_alert_device_label, self.audio_sample_rate_label,
                  self.answer_delay_label, self.max_recording_label, self.unavailable_message_label)
        text_width = max(font_metrics.width(label.text()) for label in labels)
        self.audio_input_device_label.setMinimumWidth(text_width)
        self.answer_delay_label.setMinimumWidth(text_width)

        # advanced settings
        font_metrics = self.transports_label.fontMetrics() # we assume all labels have the same font
        labels = (self.transports_label, self.media_ports_label, self.session_timeout_label, self.rtp_timeout_label, self.tls_ca_file_label, self.tls_timeout_label)
        text_width = max(font_metrics.width(label.text()) for label in labels)
        self.transports_label.setMinimumWidth(text_width)
        self.tls_ca_file_label.setMinimumWidth(text_width)

        # Adjust the combo boxes for themes with too much padding (like the default theme on Ubuntu 10.04)
        combo_box = self.audio_input_device_button
        option = QStyleOptionComboBox()
        combo_box.initStyleOption(option)
        wide_padding = (combo_box.height() - combo_box.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, combo_box).height() >= 10)
        if False and wide_padding: # TODO: review later and decide if its worth or not -Dan
            print "found wide padding"
            self.audio_alert_device_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.audio_input_device_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.audio_output_device_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.audio_sample_rate_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")
            self.unavailable_message_button.setStyleSheet("""QComboBox { padding: 4px 4px 4px 4px; }""")

    def closeEvent(self, event):
        super(PreferencesWindow, self).closeEvent(event)
        self.add_account_dialog.close()

    @property
    def account_msrp_relay(self):
        host = self.msrp_relay_host_editor.text()
        port = self.msrp_relay_port.value()
        transport = self.msrp_relay_transport_button.currentText().lower()
        return MSRPRelayAddress(host, port, transport) if host else None

    @property
    def account_outbound_proxy(self):
        host = self.outbound_proxy_host_editor.text()
        port = self.outbound_proxy_port.value()
        transport = self.outbound_proxy_transport_button.currentText().lower()
        return SIPProxyAddress(host, port, transport) if host else None

    @property
    def selected_account(self):
        try:
            selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        except IndexError:
            return None
        else:
            return selected_index.data(Qt.UserRole).account

    def _sync_defaults(self):
        settings = SIPSimpleSettings()
        account_manager = AccountManager()
        default_order = SIPSimpleSettings.rtp.audio_codec_order.default
        default_list  = SIPSimpleSettings.rtp.audio_codec_list.default

        if settings.rtp.audio_codec_order is not default_order:
            # user changed the default order, we need to sync with the new settings
            added_codecs = set(default_order).difference(settings.rtp.audio_codec_order)
            removed_codecs = set(settings.rtp.audio_codec_order).difference(default_order)
            if added_codecs:
                settings.rtp.audio_codec_order = DefaultValue # reset codec order
                settings.save()
            elif removed_codecs:
                codec_order = [codec for codec in settings.rtp.audio_codec_order if codec not in removed_codecs]
                codec_list  = [codec for codec in settings.rtp.audio_codec_list if codec not in removed_codecs]
                if codec_order == default_order:
                    codec_order = DefaultValue
                if codec_list == default_list:
                    codec_list = DefaultValue
                settings.rtp.audio_codec_order = codec_order
                settings.rtp.audio_codec_order = codec_list
                settings.save()

        for account in (account for account in account_manager.iter_accounts() if account.rtp.audio_codec_order is not None):
            # user changed the default order, we need to sync with the new settings
            added_codecs = set(default_order).difference(account.rtp.audio_codec_order)
            removed_codecs = set(account.rtp.audio_codec_order).difference(default_order)
            if added_codecs:
                account.rtp.audio_codec_order = DefaultValue # reset codec order
                account.rtp.audio_codec_list  = DefaultValue # reset codec list
                account.save()
            elif removed_codecs:
                codec_order = [codec for codec in account.rtp.audio_codec_order if codec not in removed_codecs]
                codec_list  = [codec for codec in account.rtp.audio_codec_list if codec not in removed_codecs]
                if codec_order == default_order and codec_list == default_list:
                    codec_order = DefaultValue
                    codec_list  = DefaultValue
                account.rtp.audio_codec_order = codec_order
                account.rtp.audio_codec_order = codec_list
                account.save()

    def load_audio_devices(self):
        settings = SIPSimpleSettings()

        class Separator: pass

        self.audio_input_device_button.clear()
        self.audio_input_device_button.addItem(u'System Default', 'system_default')
        self.audio_input_device_button.insertSeparator(1)
        self.audio_input_device_button.setItemData(1, Separator) # prevent the separator from being selectable
        for device in SIPApplication.engine.input_devices:
            self.audio_input_device_button.addItem(device, device)
        self.audio_input_device_button.addItem(u'None', None)
        self.audio_input_device_button.setCurrentIndex(self.audio_input_device_button.findData(settings.audio.input_device))

        self.audio_output_device_button.clear()
        self.audio_output_device_button.addItem(u'System Default', 'system_default')
        self.audio_output_device_button.insertSeparator(1)
        self.audio_output_device_button.setItemData(1, Separator) # prevent the separator from being selectable
        for device in SIPApplication.engine.output_devices:
            self.audio_output_device_button.addItem(device, device)
        self.audio_output_device_button.addItem(u'None', None)
        self.audio_output_device_button.setCurrentIndex(self.audio_output_device_button.findData(settings.audio.output_device))

        self.audio_alert_device_button.clear()
        self.audio_alert_device_button.addItem(u'System Default', 'system_default')
        self.audio_alert_device_button.insertSeparator(1)
        self.audio_alert_device_button.setItemData(1, Separator) # prevent the separator from being selectable
        for device in SIPApplication.engine.output_devices:
            self.audio_alert_device_button.addItem(device, device)
        self.audio_alert_device_button.addItem(u'None', None)
        self.audio_alert_device_button.setCurrentIndex(self.audio_alert_device_button.findData(settings.audio.alert_device))

    def load_settings(self):
        """Load settings from configuration into the UI controls"""
        settings = SIPSimpleSettings()

        self.load_in_progress = True

        # Audio devices
        self.load_audio_devices()
        self.enable_echo_cancelling_button.setChecked(settings.audio.echo_canceller.enabled)
        self.audio_sample_rate_button.clear()
        for rate in SIPSimpleSettings.audio.sample_rate.type.valid_values:
            self.audio_sample_rate_button.addItem(str(rate), rate)
        self.audio_sample_rate_button.setCurrentIndex(self.audio_sample_rate_button.findText(str(settings.audio.sample_rate)))

        # Audio codecs
        self.audio_codecs_list.clear()
        for codec in settings.rtp.audio_codec_order:
            item = QListWidgetItem(codec, self.audio_codecs_list)
            item.setCheckState(Qt.Checked if codec in settings.rtp.audio_codec_list else Qt.Unchecked)

        # Asnwering Machine settings
        self.enable_answering_machine_button.setChecked(settings.answering_machine.enabled)
        self.answer_delay.setValue(settings.answering_machine.answer_delay)
        self.max_recording.setValue(settings.answering_machine.max_recording)
        # TODO: load unavailable message -Dan

        # Chat and SMS settings
        self.auto_accept_chat_button.setChecked(settings.chat.auto_accept)
        self.sms_replication_button.setChecked(settings.chat.sms_replication)

        # File transfer settings
        self.auto_accept_files_button.setChecked(settings.file_transfer.auto_accept)
        self.download_directory_editor.setText(settings.file_transfer.directory or u'')

        # Alert settings
        self.silence_alerts_button.setChecked(settings.audio.silent)
        self.message_alerts_button.setChecked(settings.sounds.play_message_alerts)
        self.file_alerts_button.setChecked(settings.sounds.play_file_alerts)

        # File logging settings
        self.trace_sip_button.setChecked(settings.logs.trace_sip)
        self.trace_msrp_button.setChecked(settings.logs.trace_msrp)
        self.trace_xcap_button.setChecked(settings.logs.trace_xcap)
        self.trace_notifications_button.setChecked(settings.logs.trace_notifications)
        self.trace_pjsip_button.setChecked(settings.logs.trace_pjsip)
        self.pjsip_trace_level.setValue(limit(settings.logs.pjsip_level, min=0, max=5))

        # Advanced settings
        for button in self.sip_transports_button_group.buttons():
            button.setChecked(button.name in settings.sip.transport_list)

        if settings.sip.tcp_port and settings.sip.tcp_port==settings.sip.tls_port:
            log.warning("the SIP TLS and TCP ports cannot be the same")
            settings.sip.tls_port = settings.sip.tcp_port+1 if settings.sip.tcp_port<65535 else 65534
            settings.save()
        self.udp_port.setValue(settings.sip.udp_port)
        self.tcp_port.setValue(settings.sip.tcp_port)
        self.tls_port.setValue(settings.sip.tls_port)
        self.media_ports_start.setValue(settings.rtp.port_range.start)
        self.media_ports.setValue(settings.rtp.port_range.end - settings.rtp.port_range.start)

        self.session_timeout.setValue(settings.sip.invite_timeout)
        self.rtp_timeout.setValue(settings.rtp.timeout)

        self.tls_ca_file_editor.setText(settings.tls.ca_list or u'')
        self.tls_timeout.setValue(settings.tls.timeout / 1000.0)

        self.load_in_progress = False

    def load_account_settings(self, account):
        """Load the account settings from configuration into the UI controls"""
        settings = SIPSimpleSettings()
        bonjour_account = BonjourAccount()

        self.load_in_progress = True

        # Account information tab
        self.account_enabled_button.setChecked(account.enabled)
        self.account_enabled_button.setEnabled(True if account is not bonjour_account else BonjourAccount.mdns_available)
        self.display_name_editor.setText(account.display_name or u'')
        if account is not bonjour_account:
            self.password_editor.setText(account.auth.password)

        # Media tab
        self.account_audio_codecs_list.clear()
        audio_codec_order = account.rtp.audio_codec_order or settings.rtp.audio_codec_order
        audio_codec_list = account.rtp.audio_codec_list or settings.rtp.audio_codec_list
        for codec in audio_codec_order:
            item = QListWidgetItem(codec, self.account_audio_codecs_list)
            item.setCheckState(Qt.Checked if codec in audio_codec_list else Qt.Unchecked)
        self.reset_account_audio_codecs_button.setEnabled(account.rtp.audio_codec_order is not None)
        self.reset_account_video_codecs_button.setEnabled(False)
        self.account_video_codecs_list.setEnabled(False)

        self.inband_dtmf_button.setChecked(account.rtp.inband_dtmf)
        self.srtp_encryption_button.setCurrentIndex(self.srtp_encryption_button.findText(account.rtp.srtp_encryption))

        if account is not bonjour_account:
            # Server settings tab
            self.always_use_my_proxy_button.setChecked(account.sip.always_use_my_proxy)
            if account.sip.outbound_proxy is None:
                self.outbound_proxy_host_editor.setText(u'')
                self.outbound_proxy_port.setValue(5060)
                self.outbound_proxy_transport_button.setCurrentIndex(self.outbound_proxy_transport_button.findText(u'UDP'))
            else:
                self.outbound_proxy_host_editor.setText(account.sip.outbound_proxy.host)
                self.outbound_proxy_port.setValue(account.sip.outbound_proxy.port)
                self.outbound_proxy_transport_button.setCurrentIndex(self.outbound_proxy_transport_button.findText(account.sip.outbound_proxy.transport.upper()))
            self.auth_username_editor.setText(account.auth.username or u'')
            self.always_use_my_msrp_relay_button.setChecked(account.nat_traversal.use_msrp_relay_for_outbound)
            if account.nat_traversal.msrp_relay is None:
                self.msrp_relay_host_editor.setText(u'')
                self.msrp_relay_port.setValue(0)
                self.msrp_relay_transport_button.setCurrentIndex(self.msrp_relay_transport_button.findText(u'TLS'))
            else:
                self.msrp_relay_host_editor.setText(account.nat_traversal.msrp_relay.host)
                self.msrp_relay_port.setValue(account.nat_traversal.msrp_relay.port)
                self.msrp_relay_transport_button.setCurrentIndex(self.msrp_relay_transport_button.findText(account.nat_traversal.msrp_relay.transport.upper()))
            self.voicemail_uri_editor.setText(account.message_summary.voicemail_uri or u'')
            self.xcap_root_editor.setText(account.xcap.xcap_root or u'')
            self.server_tools_url_editor.setText(account.server.settings_url or u'')
            self.conference_server_editor.setText(account.server.conference_server or u'')

            # Network tab
            self.use_ice_button.setChecked(account.nat_traversal.use_ice)
            self.msrp_transport_button.setCurrentIndex(self.msrp_transport_button.findText(account.msrp.transport.upper()))

            # Advanced tab
            self.register_interval.setValue(account.sip.register_interval)
            self.publish_interval.setValue(account.sip.publish_interval)
            self.subscribe_interval.setValue(account.sip.subscribe_interval)
            self.reregister_button.setEnabled(account.enabled)

            item_text = account.pstn.idd_prefix or '+'
            index = self.idd_prefix_button.findText(item_text)
            if index == -1:
                self.idd_prefix_button.addItem(item_text)
            self.idd_prefix_button.setCurrentIndex(self.idd_prefix_button.findText(item_text))

            item_text = account.pstn.prefix or 'None'
            index = self.prefix_button.findText(item_text)
            if index == -1:
                self.prefix_button.addItem(item_text)
            self.prefix_button.setCurrentIndex(self.prefix_button.findText(item_text))

            self._update_pstn_example_label()

            self.account_tls_cert_file_editor.setText(account.tls.certificate or u'')
            self.account_tls_verify_server_button.setChecked(account.tls.verify_server)

        self.load_in_progress = False

    def show(self):
        selection_model = self.account_list.selectionModel()
        if not selection_model.selectedIndexes():
            model = self.account_list.model()
            account_manager = AccountManager()
            default_account = account_manager.default_account
            try:
                index = (index for index, account in enumerate(model.accounts) if account==default_account).next()
            except StopIteration:
                index = 0
            selection_model.select(model.index(index), selection_model.ClearAndSelect)
        self._update_logs_size_label()
        super(PreferencesWindow, self).show()
        self.raise_()
        self.activateWindow()

    def show_for_accounts(self):
        self.accounts_action.trigger()
        self.show()

    def show_add_account_dialog(self):
        self.add_account_dialog.open_for_add()

    def show_create_account_dialog(self):
        self.add_account_dialog.open_for_create()

    @staticmethod
    def _normalize_binary_size(size):
        """Return a human friendly string representation of size as a power of 2"""
        infinite = float('infinity')
        boundaries = [(             1024, '%d bytes',               1),
                      (          10*1024, '%.2f KB',           1024.0),  (     1024*1024, '%.1f KB',           1024.0),
                      (     10*1024*1024, '%.2f MB',      1024*1024.0),  (1024*1024*1024, '%.1f MB',      1024*1024.0),
                      (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (      infinite, '%.1f GB', 1024*1024*1024.0)]
        for boundary, format, divisor in boundaries:
            if size < boundary:
                return format % (size/divisor,)
        else:
            return "%d bytes" % size

    def _update_logs_size_label(self):
        logs_size = 0
        for path, dirs, files in os.walk(os.path.join(ApplicationData.directory, 'logs')):
            for name in dirs:
                try:
                    logs_size += os.stat(os.path.join(path, name)).st_size
                except (OSError, IOError):
                    pass
            for name in files:
                try:
                    logs_size += os.stat(os.path.join(path, name)).st_size
                except (OSError, IOError):
                    pass
        self.log_files_size_label.setText(u"There are currently %s of log files" % self._normalize_binary_size(logs_size))

    def _update_pstn_example_label(self):
        prefix = self.prefix_button.currentText()
        idd_prefix = self.idd_prefix_button.currentText()
        self.pstn_example_transformed_label.setText(u"%s%s442079460000" % ('' if prefix=='None' else prefix, idd_prefix))

    # Signal handlers
    #
    def _SH_ToolbarActionTriggered(self, action):
        if action == self.logging_action:
            self._update_logs_size_label()
        self.pages.setCurrentIndex(action.index)

    def _SH_AccountListSelectionChanged(self, selected, deselected):
        try:
            selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        except IndexError:
            self.delete_account_button.setEnabled(False)
            self.account_tab_widget.setEnabled(False)
        else:
            selected_account = selected_index.data(Qt.UserRole).account
            self.delete_account_button.setEnabled(selected_account is not BonjourAccount())
            tab_widget = self.account_tab_widget
            tab_widget.setEnabled(True)
            if selected_account is BonjourAccount():
                tab_widget.removeTab(tab_widget.indexOf(self.server_settings_tab))
                tab_widget.removeTab(tab_widget.indexOf(self.network_tab))
                tab_widget.removeTab(tab_widget.indexOf(self.advanced_tab))
                self.password_label.hide()
                self.password_editor.hide()
            else:
                if tab_widget.indexOf(self.server_settings_tab) == -1:
                    tab_widget.addTab(self.server_settings_tab, u"Server Settings")
                if tab_widget.indexOf(self.network_tab) == -1:
                    tab_widget.addTab(self.network_tab, u"Network")
                if tab_widget.indexOf(self.advanced_tab) == -1:
                    tab_widget.addTab(self.advanced_tab, u"Advanced")
                self.password_label.show()
                self.password_editor.show()
                self.voicemail_uri_editor.inactiveText = u"Discovered by subscribing to %s" % selected_account.id
                self.xcap_root_editor.inactiveText = u"Taken from the DNS TXT record for xcap.%s" % selected_account.id.domain
            self.load_account_settings(selected_account)

    def _SH_DeleteAccountButtonClicked(self):
        model = self.account_list.model()

        selected_index = self.account_list.selectionModel().selectedIndexes()[0]
        selected_account = selected_index.data(Qt.UserRole).account

        title, message = u"Remove Account", u"Permanently remove account %s?" % selected_account.id
        if QMessageBox.question(self, title, message, QMessageBox.Ok|QMessageBox.Cancel) == QMessageBox.Cancel:
            return

        account_manager = AccountManager()
        if account_manager.default_account is selected_account:
            active_accounts = [account_info.account for account_info in model.accounts if account_info.account.enabled]
            position = active_accounts.index(selected_account)
            if position < len(active_accounts)-1:
                account_manager.default_account = active_accounts[position+1]
            elif position > 0:
                account_manager.default_account = active_accounts[position-1]
            else:
                account_manager.default_account = None

        try:
            os.unlink(selected_account.tls.certificate.normalized)
        except (AttributeError, OSError, IOError):
            pass

        selected_account.delete()

    # Account information
    def _SH_AccountEnabledButtonClicked(self, checked):
        account = self.selected_account
        account.enabled = checked
        account.save()

    def _SH_DisplayNameEditorEditingFinished(self):
        account = self.selected_account
        display_name = self.display_name_editor.text() or None
        if account.display_name != display_name:
            account.display_name = display_name
            account.save()

    def _SH_PasswordEditorEditingFinished(self):
        account = self.selected_account
        password = self.password_editor.text()
        if account.auth.password != password:
            account.auth.password = password
            account.save()

    # Account media settings
    def _SH_AccountAudioCodecsListItemChanged(self, item):
        if not self.load_in_progress:
            account = self.selected_account
            items = [self.account_audio_codecs_list.item(row) for row in xrange(self.account_audio_codecs_list.count())]
            account.rtp.audio_codec_list = [item.text() for item in items if item.checkState()==Qt.Checked]
            account.rtp.audio_codec_order = [item.text() for item in items]
            account.save()

    def _SH_AccountAudioCodecsListModelRowsInserted(self, parent, start, end):
        if not self.load_in_progress:
            account = self.selected_account
            items = [self.account_audio_codecs_list.item(row) for row in xrange(self.account_audio_codecs_list.count())]
            account.rtp.audio_codec_order = [item.text() for item in items]
            account.rtp.audio_codec_list = [item.text() for item in items if item.checkState()==Qt.Checked]
            account.save()

    def _SH_AccountAudioCodecsListModelRowsMoved(self, source_parent, source_start, source_end, dest_parent, dest_row):
        account = self.selected_account
        items = [self.account_audio_codecs_list.item(row) for row in xrange(self.account_audio_codecs_list.count())]
        account.rtp.audio_codec_order = [item.text() for item in items]
        account.rtp.audio_codec_list = [item.text() for item in items if item.checkState()==Qt.Checked]
        account.save()

    def _SH_ResetAudioCodecsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        account = self.selected_account

        self.load_in_progress = True

        self.account_audio_codecs_list.clear()
        audio_codec_order = settings.rtp.audio_codec_order
        audio_codec_list = settings.rtp.audio_codec_list
        for codec in audio_codec_order:
            item = QListWidgetItem(codec, self.account_audio_codecs_list)
            item.setCheckState(Qt.Checked if codec in audio_codec_list else Qt.Unchecked)

        self.load_in_progress = False

        account.rtp.audio_codec_list  = DefaultValue
        account.rtp.audio_codec_order = DefaultValue
        account.save()

    def _SH_InbandDTMFButtonClicked(self, checked):
        account = self.selected_account
        account.rtp.inband_dtmf = checked
        account.save()

    def _SH_SRTPEncryptionButtonActivated(self, text):
        account = self.selected_account
        account.rtp.srtp_encryption = text
        account.save()

    # Account server settings
    def _SH_AlwaysUseMyProxyButtonClicked(self, checked):
        account = self.selected_account
        account.sip.always_use_my_proxy = checked
        account.save()

    def _SH_OutboundProxyHostEditorEditingFinished(self):
        account = self.selected_account
        outbound_proxy = self.account_outbound_proxy
        if account.sip.outbound_proxy != outbound_proxy:
            account.sip.outbound_proxy = outbound_proxy
            account.save()

    def _SH_OutboundProxyPortValueChanged(self, value):
        if not self.load_in_progress:
            account = self.selected_account
            outbound_proxy = self.account_outbound_proxy
            if account.sip.outbound_proxy != outbound_proxy:
                account.sip.outbound_proxy = outbound_proxy
                account.save()

    def _SH_OutboundProxyTransportButtonActivated(self, text):
        account = self.selected_account
        outbound_proxy = self.account_outbound_proxy
        if account.sip.outbound_proxy != outbound_proxy:
            account.sip.outbound_proxy = outbound_proxy
            account.save()

    def _SH_AuthUsernameEditorEditingFinished(self):
        account = self.selected_account
        auth_username = self.auth_username_editor.text() or None
        if account.auth.username != auth_username:
            account.auth.username = auth_username
            account.save()

    def _SH_AlwaysUseMyMSRPRelayButtonClicked(self, checked):
        account = self.selected_account
        account.nat_traversal.use_msrp_relay_for_outbound = checked
        account.save()

    def _SH_MSRPRelayHostEditorEditingFinished(self):
        account = self.selected_account
        msrp_relay = self.account_msrp_relay
        if account.nat_traversal.msrp_relay != msrp_relay:
            account.nat_traversal.msrp_relay = msrp_relay
            account.save()

    def _SH_MSRPRelayPortValueChanged(self, value):
        if not self.load_in_progress:
            account = self.selected_account
            msrp_relay = self.account_msrp_relay
            if account.nat_traversal.msrp_relay != msrp_relay:
                account.nat_traversal.msrp_relay = msrp_relay
                account.save()

    def _SH_MSRPRelayTransportButtonActivated(self, text):
        account = self.selected_account
        msrp_relay = self.account_msrp_relay
        if account.nat_traversal.msrp_relay != msrp_relay:
            account.nat_traversal.msrp_relay = msrp_relay
            account.save()

    def _SH_VoicemailURIEditorEditingFinished(self):
        account = self.selected_account
        voicemail_uri = self.voicemail_uri_editor.text() or None
        if account.message_summary.voicemail_uri != voicemail_uri:
            account.message_summary.voicemail_uri = voicemail_uri
            account.save()

    def _SH_XCAPRootEditorEditingFinished(self):
        account = self.selected_account
        xcap_root = self.xcap_root_editor.text() or None
        if account.xcap.xcap_root != xcap_root:
            account.xcap.xcap_root = xcap_root
            account.save()

    def _SH_ServerToolsURLEditorEditingFinished(self):
        account = self.selected_account
        url = self.server_tools_url_editor.text() or None
        if account.server.settings_url != url:
            account.server.settings_url = url
            account.save()

    def _SH_ConferenceServerEditorEditingFinished(self):
        account = self.selected_account
        server = self.conference_server_editor.text() or None
        if account.server.conference_server != server:
            account.server.conference_server = server
            account.save()

    # Account network settings
    def _SH_UseICEButtonClicked(self, checked):
        account = self.selected_account
        account.nat_traversal.use_ice = checked
        account.save()

    def _SH_MSRPTransportButtonActivated(self, text):
        account = self.selected_account
        account.msrp.transport = text.lower()
        account.save()

    # Account advanced settings
    def _SH_RegisterIntervalValueChanged(self, value):
        if not self.load_in_progress:
            account = self.selected_account
            account.sip.register_interval = value
            account.save()

    def _SH_PublishIntervalValueChanged(self, value):
        if not self.load_in_progress:
            account = self.selected_account
            account.sip.publish_interval = value
            account.save()

    def _SH_SubscribeIntervalValueChanged(self, value):
        if not self.load_in_progress:
            account = self.selected_account
            account.sip.subscribe_interval = value
            account.save()

    def _SH_ReregisterButtonClicked(self):
        account = self.selected_account
        account.reregister()

    def _SH_IDDPrefixButtonActivated(self, text):
        self._update_pstn_example_label()
        account = self.selected_account
        idd_prefix = None if text=='+' else text
        if account.pstn.idd_prefix != idd_prefix:
            account.pstn.idd_prefix = idd_prefix
            account.save()

    def _SH_PrefixButtonActivated(self, text):
        self._update_pstn_example_label()
        account = self.selected_account
        prefix = None if text=='None' else text
        if account.pstn.prefix != prefix:
            account.pstn.prefix = prefix
            account.save()

    def _SH_AccountTLSCertFileEditorLocationCleared(self):
        account = self.selected_account
        account.tls.certificate = None
        account.save()

    def _SH_AccountTLSCertFileBrowseButtonClicked(self, checked):
        # TODO: open the file selection dialog in non-modal mode (and the error messages boxes as well). -Dan
        account = self.selected_account
        directory = os.path.dirname(account.tls.certificate.normalized) if account.tls.certificate else os.path.expanduser('~')
        cert_path = QFileDialog.getOpenFileName(self, u'Select Certificate File', directory, u"TLS certificates (*.crt *.pem)") or None
        if cert_path is not None:
            cert_path = os.path.normpath(cert_path)
            if cert_path != account.tls.certificate:
                try:
                    contents = open(cert_path).read()
                    X509Certificate(contents)
                    X509PrivateKey(contents)
                except (OSError, IOError), e:
                    QMessageBox.critical(self, u"TLS Certificate Error", u"The certificate file could not be opened: %s" % e.strerror)
                except GNUTLSError, e:
                    QMessageBox.critical(self, u"TLS Certificate Error", u"The certificate file is invalid: %s" % e)
                else:
                    self.account_tls_cert_file_editor.setText(cert_path)
                    account.tls.certificate = cert_path
                    account.save()

    def _SH_AccountTLSVerifyServerButtonClicked(self, checked):
        account = self.selected_account
        account.tls.verify_server = checked
        account.save()

    # Audio devices signal handlers
    def _SH_AudioAlertDeviceButtonActivated(self, index):
        device = self.audio_alert_device_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.audio.alert_device = device
        settings.save()

    def _SH_AudioInputDeviceButtonActivated(self, index):
        device = self.audio_input_device_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.audio.input_device = device
        settings.save()

    def _SH_AudioOutputDeviceButtonActivated(self, index):
        device = self.audio_output_device_button.itemData(index)
        settings = SIPSimpleSettings()
        settings.audio.output_device = device
        settings.save()

    def _SH_AudioSampleRateButtonActivated(self, text):
        settings = SIPSimpleSettings()
        settings.audio.sample_rate = text
        settings.save()

    def _SH_EnableEchoCancellingButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.audio.echo_canceller.enabled = checked
        settings.save()

    # Audio codecs signal handlers
    def _SH_AudioCodecsListItemChanged(self, item):
        if not self.load_in_progress:
            settings = SIPSimpleSettings()
            item_iterator = (self.audio_codecs_list.item(row) for row in xrange(self.audio_codecs_list.count()))
            settings.rtp.audio_codec_list = [item.text() for item in item_iterator if item.checkState()==Qt.Checked]
            settings.save()

    def _SH_AudioCodecsListModelRowsInserted(self, parent, start, end):
        if not self.load_in_progress:
            settings = SIPSimpleSettings()
            items = [self.audio_codecs_list.item(row) for row in xrange(self.audio_codecs_list.count())]
            settings.rtp.audio_codec_order = [item.text() for item in items]
            settings.rtp.audio_codec_list = [item.text() for item in items if item.checkState()==Qt.Checked]
            settings.save()

    def _SH_AudioCodecsListModelRowsMoved(self, source_parent, source_start, source_end, dest_parent, dest_row):
        settings = SIPSimpleSettings()
        items = [self.audio_codecs_list.item(row) for row in xrange(self.audio_codecs_list.count())]
        settings.rtp.audio_codec_order = [item.text() for item in items]
        settings.rtp.audio_codec_list = [item.text() for item in items if item.checkState()==Qt.Checked]
        settings.save()

    # Answering machine signal handlers
    def _SH_EnableAnsweringMachineButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.answering_machine.enabled = checked
        settings.save()

    def _SH_AnswerDelayValueChanged(self, value):
        if value == 0:
            self.answer_delay_seconds_label.setText(u'')
        elif value == 1:
            self.answer_delay_seconds_label.setText(u'second')
        else:
            self.answer_delay_seconds_label.setText(u'seconds')
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.answering_machine.answer_delay != value:
            settings.answering_machine.answer_delay = value
            settings.save()

    def _SH_MaxRecordingValueChanged(self, value):
        self.max_recording_minutes_label.setText(u'minute' if value==1 else u'minutes')
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.answering_machine.max_recording != value:
            settings.answering_machine.max_recording = value
            settings.save()

    # Chat and SMS signal handlers
    def _SH_AutoAcceptChatButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.chat.auto_accept = checked
        settings.save()

    def _SH_SMSReplicationButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.chat.sms_replication = checked
        settings.save()

    # File transfer signal handlers
    def _SH_AutoAcceptFilesButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.file_transfer.auto_accept = checked
        settings.save()

    def _SH_DownloadDirectoryEditorLocationCleared(self):
        settings = SIPSimpleSettings()
        settings.file_transfer.directory = None
        settings.save()

    def _SH_DownloadDirectoryBrowseButtonClicked(self, checked):
        # TODO: open the file selection dialog in non-modal mode. Same for the one for TLS CA list and the IconSelector from contacts. -Dan
        settings = SIPSimpleSettings()
        directory = settings.file_transfer.directory or os.path.expanduser('~')
        directory = QFileDialog.getExistingDirectory(self, u'Select Download Directory', directory) or None
        if directory is not None:
            directory = os.path.normpath(directory)
            if directory != settings.file_transfer.directory:
                self.download_directory_editor.setText(directory)
                settings.file_transfer.directory = directory
                settings.save()

    # Alerts signal handlers
    def _SH_SilenceAlertsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.audio.silent = checked
        settings.save()

    def _SH_MessageAlertsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.sounds.play_message_alerts = checked
        settings.save()

    def _SH_FileAlertsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.sounds.play_file_alerts = checked
        settings.save()

    # File logging signal handlers
    def _SH_TraceSIPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_sip = checked
        settings.save()

    def _SH_TraceMSRPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_msrp = checked
        settings.save()

    def _SH_TraceXCAPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_xcap = checked
        settings.save()

    def _SH_TraceNotificationsButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_notifications = checked
        settings.save()

    def _SH_TracePJSIPButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.logs.trace_pjsip = checked
        settings.save()

    def _SH_PJSIPTraceLevelValueChanged(self, value):
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.logs.pjsip_level != value:
            settings.logs.pjsip_level = value
            settings.save()

    @run_in_thread('file-io')
    def _SH_ClearLogFilesButtonClicked(self):
        log_manager = LogManager()
        log_manager.stop()
        for path, dirs, files in os.walk(os.path.join(ApplicationData.directory, 'logs'), topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(path, name))
                except (OSError, IOError):
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(path, name))
                except (OSError, IOError):
                    pass
        log_manager.start()
        call_in_gui_thread(self._update_logs_size_label)

    # SIP and RTP signal handlers
    def _SH_SIPTransportsButtonClicked(self, button):
        settings = SIPSimpleSettings()
        settings.sip.transport_list = [button.name for button in self.sip_transports_button_group.buttons() if button.isChecked()]
        settings.save()

    def _SH_UDPPortValueChanged(self, value):
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.sip.udp_port != value:
            settings.sip.udp_port = value
            settings.save()

    def _SH_TCPPortValueChanged(self, value):
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.sip.tcp_port != value:
            settings.sip.tcp_port = value
            settings.save()

    def _SH_TLSPortValueChanged(self, value):
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.sip.tls_port != value:
            settings.sip.tls_port = value
            settings.save()

    def _SH_MediaPortsStartValueChanged(self, value):
        self.media_ports.setMaximum(limit(65535-value, min=10, max=10000))
        settings = SIPSimpleSettings()
        port_range = PortRange(value, value + self.media_ports.value())
        if not self.load_in_progress and settings.rtp.port_range != port_range:
            settings.rtp.port_range = port_range
            settings.save()

    def _SH_MediaPortsValueChanged(self, value):
        self.media_ports_start.setMaximum(limit(65535-value, min=10000, max=65000))
        settings = SIPSimpleSettings()
        port_range = PortRange(self.media_ports_start.value(), self.media_ports_start.value() + value)
        if not self.load_in_progress and settings.rtp.port_range != port_range:
            settings.rtp.port_range = port_range
            settings.save()

    def _SH_SessionTimeoutValueChanged(self, value):
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.sip.invite_timeout != value:
            settings.sip.invite_timeout = value
            settings.save()

    def _SH_RTPTimeoutValueChanged(self, value):
        if value == 0:
            self.rtp_timeout_seconds_label.setText(u'')
        elif value == 1:
            self.rtp_timeout_seconds_label.setText(u'second')
        else:
            self.rtp_timeout_seconds_label.setText(u'seconds')
        settings = SIPSimpleSettings()
        if not self.load_in_progress and settings.rtp.timeout != value:
            settings.rtp.timeout = value
            settings.save()

    # TLS signal handlers
    def _SH_TLSCAFileEditorLocationCleared(self):
        settings = SIPSimpleSettings()
        settings.tls.ca_list = None
        settings.save()

    def _SH_TLSCAFileBrowseButtonClicked(self):
        # TODO: open the file selection dialog in non-modal mode (and the error messages boxes as well). -Dan
        settings = SIPSimpleSettings()
        directory = os.path.dirname(settings.tls.ca_list.normalized) if settings.tls.ca_list else os.path.expanduser('~')
        ca_path = QFileDialog.getOpenFileName(self, u'Select Certificate Authority File', directory, u"TLS certificates (*.crt *.pem)") or None
        if ca_path is not None:
            ca_path = os.path.normpath(ca_path)
            if ca_path != settings.tls.ca_list:
                try:
                    X509Certificate(open(ca_path).read())
                except (OSError, IOError), e:
                    QMessageBox.critical(self, u"TLS Certificate Error", u"The certificate authority file could not be opened: %s" % e.strerror)
                except GNUTLSError, e:
                    QMessageBox.critical(self, u"TLS Certificate Error", u"The certificate authority file is invalid: %s" % e)
                else:
                    self.tls_ca_file_editor.setText(ca_path)
                    settings.tls.ca_list = ca_path
                    settings.save()

    def _SH_TLSTimeoutValueChanged(self, value):
        self.tls_timeout_seconds_label.setText(u'second' if value==1 else u'seconds')
        settings = SIPSimpleSettings()
        timeout = value * 1000
        if not self.load_in_progress and settings.tls.timeout != timeout:
            settings.tls.timeout = timeout
            settings.save()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        self._sync_defaults()
        self.load_settings()
        notification.center.add_observer(self, name='AudioDevicesDidChange')
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange')

    def _NH_AudioDevicesDidChange(self, notification):
        self.load_audio_devices()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.sender is settings:
            if 'audio.silent' in notification.data.modified:
                self.silence_alerts_button.setChecked(settings.audio.silent)
            if 'audio.alert_device' in notification.data.modified:
                self.audio_alert_device_button.setCurrentIndex(self.audio_alert_device_button.findData(settings.audio.alert_device))
            if 'audio.input_device' in notification.data.modified:
                self.audio_input_device_button.setCurrentIndex(self.audio_input_device_button.findData(settings.audio.input_device))
            if 'audio.output_device' in notification.data.modified:
                self.audio_output_device_button.setCurrentIndex(self.audio_output_device_button.findData(settings.audio.output_device))
            if 'answering_machine.enabled' in notification.data.modified:
                self.enable_answering_machine_button.setChecked(settings.answering_machine.enabled)
            if 'chat.auto_accept' in notification.data.modified:
                self.auto_accept_chat_button.setChecked(settings.chat.auto_accept)
            if 'file_transfer.auto_accept' in notification.data.modified:
                self.auto_accept_files_button.setChecked(settings.file_transfer.auto_accept)
        elif isinstance(notification.sender, (Account, BonjourAccount)) and notification.sender is self.selected_account:
            account = notification.sender
            if 'enabled' in notification.data.modified:
                self.account_enabled_button.setChecked(account.enabled)
                self.reregister_button.setEnabled(account.enabled)
            if 'display_name' in notification.data.modified:
                self.display_name_editor.setText(account.display_name or u'')
            if 'rtp.audio_codec_list' in notification.data.modified:
                self.reset_account_audio_codecs_button.setEnabled(account.rtp.audio_codec_list is not None)

del ui_class, base_class


