# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['AccountModel', 'ActiveAccountModel', 'AccountSelector', 'AddAccountDialog']

import os
import re
import sys
import urllib
import urllib2
from collections import defaultdict

from PyQt4 import uic
from PyQt4.QtCore import Qt, QAbstractListModel, QModelIndex
from PyQt4.QtGui  import QButtonGroup, QComboBox, QIcon, QPalette, QPixmap, QSortFilterProxyModel, QStyledItemDelegate

import cjson
from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from gnutls.errors import GNUTLSError
from zope.interface import implements

from sipsimple.account import Account, AccountExists, AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.util import user_info

from blink.resources import Resources
from blink.widgets.labels import Status
from blink.util import QSingleton, call_in_auxiliary_thread, call_in_gui_thread, run_in_auxiliary_thread, run_in_gui_thread


class AccountInfo(object):
    def __init__(self, name, account, icon=None):
        self.name = name
        self.account = account
        self.icon = icon
        self.registration_state = None

    def __eq__(self, other):
        if isinstance(other, basestring):
            return self.name == other
        elif isinstance(other, (Account, BonjourAccount)):
            return self.account == other
        return False

    def __ne__(self, other):
        return not self.__eq__(other)


class AccountModel(QAbstractListModel):
    implements(IObserver)

    def __init__(self, parent=None):
        super(AccountModel, self).__init__(parent)
        self.accounts = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPAccountDidActivate')
        notification_center.add_observer(self, name='SIPAccountDidDeactivate')
        notification_center.add_observer(self, name='SIPAccountWillRegister')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidFail')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidEnd')
        notification_center.add_observer(self, name='BonjourAccountWillRegister')
        notification_center.add_observer(self, name='BonjourAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='BonjourAccountRegistrationDidFail')
        notification_center.add_observer(self, name='BonjourAccountRegistrationDidEnd')
        notification_center.add_observer(self, sender=AccountManager())

    def rowCount(self, parent=QModelIndex()):
        return len(self.accounts)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        account_info = self.accounts[index.row()]
        if role == Qt.DisplayRole:
            return account_info.name
        elif role == Qt.DecorationRole:
            return account_info.icon
        elif role == Qt.UserRole:
            return account_info
        return None

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        name = u'Bonjour' if account is BonjourAccount() else unicode(account.id)
        icon = None
        if account is BonjourAccount():
            pixmap = QPixmap()
            if pixmap.load(Resources.get('icons/bonjour.png')):
                pixmap = pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                icon = QIcon(pixmap)
        self.beginInsertRows(QModelIndex(), len(self.accounts), len(self.accounts))
        self.accounts.append(AccountInfo(name, account, icon))
        self.endInsertRows()

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        position = self.accounts.index(notification.data.account)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.accounts[position]
        self.endRemoveRows()

    def _NH_SIPAccountDidActivate(self, notification):
        position = self.accounts.index(notification.sender)
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountDidDeactivate(self, notification):
        position = self.accounts.index(notification.sender)
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountWillRegister(self, notification):
        position = self.accounts.index(notification.sender)
        self.accounts[position].registration_state = 'started'
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        position = self.accounts.index(notification.sender)
        self.accounts[position].registration_state = 'succeeded'
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        position = self.accounts.index(notification.sender)
        self.accounts[position].registration_state = 'failed'
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        position = self.accounts.index(notification.sender)
        self.accounts[position].registration_state = 'ended'
        self.dataChanged.emit(self.index(position), self.index(position))

    _NH_BonjourAccountWillRegister = _NH_SIPAccountWillRegister
    _NH_BonjourAccountRegistrationDidSucceed = _NH_SIPAccountRegistrationDidSucceed
    _NH_BonjourAccountRegistrationDidFail = _NH_SIPAccountRegistrationDidFail
    _NH_BonjourAccountRegistrationDidEnd = _NH_SIPAccountRegistrationDidEnd


class ActiveAccountModel(QSortFilterProxyModel):
    def __init__(self, model, parent=None):
        super(ActiveAccountModel, self).__init__(parent)
        self.setSourceModel(model)
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        account_info = source_model.data(source_index, Qt.UserRole)
        return account_info.account.enabled


class AccountDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        account_info = index.data(Qt.UserRole).toPyObject()
        if account_info.registration_state == 'succeeded':
            option.palette.setColor(QPalette.Text, Qt.black)
        else:
            option.palette.setColor(QPalette.Text, Qt.gray)
        super(AccountDelegate, self).paint(painter, option, index)


class AccountSelector(QComboBox):
    implements(IObserver)

    def __init__(self, parent=None):
        super(AccountSelector, self).__init__(parent)
        self.currentIndexChanged[int].connect(self.selection_changed)
        self.model().dataChanged.connect(self.data_changed)
        self.view().setItemDelegate(AccountDelegate(self.view()))

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="SIPAccountManagerDidChangeDefaultAccount")
        notification_center.add_observer(self, name="SIPAccountManagerDidStart")

    def setModel(self, model):
        self.model().dataChanged.disconnect(self.data_changed)
        model.dataChanged.connect(self.data_changed)
        super(AccountSelector, self).setModel(model)

    def data_changed(self, topLeft, bottomRight):
        index = self.currentIndex()
        if topLeft.row() <= index <= bottomRight.row():
            account_info = self.itemData(index).toPyObject()
            palette = self.palette()
            if account_info.registration_state == 'succeeded':
                palette.setColor(QPalette.Text, Qt.black)
            else:
                palette.setColor(QPalette.Text, Qt.gray)
            self.setPalette(palette)

    def selection_changed(self, index):
        if index == -1:
            return
        account_info = self.itemData(index).toPyObject()
        palette = self.palette()
        if account_info.registration_state == 'succeeded':
            palette.setColor(QPalette.Text, Qt.black)
        else:
            palette.setColor(QPalette.Text, Qt.gray)
        self.setPalette(palette)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidStart(self, notification):
        account = AccountManager().default_account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        account = notification.data.account
        if account is not None:
            model = self.model()
            source_model = model.sourceModel()
            account_index = source_model.accounts.index(account)
            self.setCurrentIndex(model.mapFromSource(source_model.index(account_index)).row())


ui_class, base_class = uic.loadUiType(Resources.get('add_account.ui'))

class AddAccountDialog(base_class, ui_class):
    __metaclass__ = QSingleton

    implements(IObserver)

    def __init__(self, parent=None):
        super(AddAccountDialog, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.background_frame.setStyleSheet("")
        self.button_group = QButtonGroup(self)
        self.button_group.setObjectName("button_group")
        self.button_group.addButton(self.add_account_button, self.panel_view.indexOf(self.add_account_panel))
        self.button_group.addButton(self.create_account_button, self.panel_view.indexOf(self.create_account_panel))
        font = self.title_label.font()
        font.setPointSizeF(self.info_label.fontInfo().pointSizeF() + 3)
        font.setFamily("Sans Serif")
        self.title_label.setFont(font)
        font_metrics = self.create_status_label.fontMetrics()
        self.create_status_label.setMinimumHeight(font_metrics.height() + 2*(font_metrics.height() + font_metrics.leading())) # reserve space for 3 lines
        font_metrics = self.email_note_label.fontMetrics()
        self.email_note_label.setMinimumWidth(font_metrics.width(u'The E-mail address is used when sending voicemail')) # hack to make text justification look nice everywhere
        self.add_account_button.setChecked(True)
        self.panel_view.setCurrentWidget(self.add_account_panel)
        self.new_password_editor.textChanged.connect(self._SH_PasswordTextChanged)
        self.button_group.buttonClicked[int].connect(self._SH_PanelChangeRequest)
        self.accept_button.clicked.connect(self._SH_AcceptButtonClicked)
        self.display_name_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.name_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.username_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.sip_address_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.password_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.new_password_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.verify_password_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.email_address_editor.statusChanged.connect(self._SH_ValidityStatusChanged)
        self.display_name_editor.regexp = re.compile('^.*$')
        self.name_editor.regexp = re.compile('^.+$')
        self.username_editor.regexp = re.compile('^\w(?<=[^0_])[\w.-]{3,30}(?<=[^_.-])$', re.IGNORECASE) # in order to enable unicode characters add re.UNICODE to flags
        self.sip_address_editor.regexp = re.compile('^[^@\s]+@[^@\s]+$')
        self.password_editor.regexp = re.compile('^.*$')
        self.new_password_editor.regexp = re.compile('^.{8,}$')
        self.verify_password_editor.regexp = re.compile('^$')
        self.email_address_editor.regexp = re.compile('^[^@\s]+@[^@\s]+$')

        account_manager = AccountManager()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=account_manager)

    def _get_display_name(self):
        if self.panel_view.currentWidget() is self.add_account_panel:
            return unicode(self.display_name_editor.text())
        else:
            return unicode(self.name_editor.text())

    def _set_display_name(self, value):
        self.display_name_editor.setText(value)
        self.name_editor.setText(value)

    def _get_username(self):
        return unicode(self.username_editor.text())

    def _set_username(self, value):
        self.username_editor.setText(value)

    def _get_sip_address(self):
        return unicode(self.sip_address_editor.text())

    def _set_sip_address(self, value):
        self.sip_address_editor.setText(value)

    def _get_password(self):
        if self.panel_view.currentWidget() is self.add_account_panel:
            return unicode(self.password_editor.text())
        else:
            return unicode(self.new_password_editor.text())

    def _set_password(self, value):
        self.password_editor.setText(value)
        self.new_password_editor.setText(value)

    def _get_verify_password(self):
        return unicode(self.verify_password_editor.text())

    def _set_verify_password(self, value):
        self.verify_password_editor.setText(value)

    def _get_email_address(self):
        return unicode(self.email_address_editor.text())

    def _set_email_address(self, value):
        self.email_address_editor.setText(value)

    display_name    = property(_get_display_name, _set_display_name)
    username        = property(_get_username, _set_username)
    sip_address     = property(_get_sip_address, _set_sip_address)
    password        = property(_get_password, _set_password)
    verify_password = property(_get_verify_password, _set_verify_password)
    email_address   = property(_get_email_address, _set_email_address)

    del _get_display_name, _set_display_name, _get_username, _set_username
    del _get_sip_address, _set_sip_address, _get_email_address, _set_email_address
    del _get_password, _set_password, _get_verify_password, _set_verify_password

    def _SH_AcceptButtonClicked(self):
        if self.panel_view.currentWidget() is self.add_account_panel:
            account = Account(self.sip_address)
            account.enabled = True
            account.display_name = self.display_name
            account.auth.password = self.password
            call_in_auxiliary_thread(account.save)
            account_manager = AccountManager()
            account_manager.default_account = account
            self.accept()
        else:
            self.setEnabled(False)
            self.create_status_label.value = Status('Creating account on server...')
            self._create_sip_account(self.username, self.password, self.email_address, self.display_name)

    def _SH_PanelChangeRequest(self, index):
        self.panel_view.setCurrentIndex(index)
        if self.panel_view.currentWidget() is self.add_account_panel:
            inputs = [self.display_name_editor, self.sip_address_editor, self.password_editor]
        else:
            inputs = [self.name_editor, self.username_editor, self.new_password_editor, self.verify_password_editor, self.email_address_editor]
        self.accept_button.setEnabled(all(input.text_valid for input in inputs))

    def _SH_PasswordTextChanged(self, text):
        self.verify_password_editor.regexp = re.compile(u'^%s$' % re.escape(unicode(text)))

    def _SH_ValidityStatusChanged(self):
        red = '#cc0000'
        # validate the add panel
        if not self.display_name_editor.text_valid:
            self.add_status_label.value = Status("Display name cannot be empty", color=red)
        elif not self.sip_address_editor.text_correct:
            self.add_status_label.value = Status("SIP address should be specified as user@domain", color=red)
        elif not self.sip_address_editor.text_allowed:
            self.add_status_label.value = Status("An account with this SIP address was already added", color=red)
        elif not self.password_editor.text_valid:
            self.add_status_label.value = Status("Password cannot be empty", color=red)
        else:
            self.add_status_label.value = None
        # validate the create panel
        if not self.name_editor.text_valid:
            self.create_status_label.value = Status("Name cannot be empty", color=red)
        elif not self.username_editor.text_correct:
            self.create_status_label.value = Status("Username should have 5 to 32 characters, start with a letter or non-zero digit, contain only letters, digits or .-_ and end with a letter or digit", color=red)
        elif not self.username_editor.text_allowed:
            self.create_status_label.value = Status("The username you requested is already taken. Please choose another one and try again.", color=red)
        elif not self.new_password_editor.text_valid:
            self.create_status_label.value = Status("Password should contain at least 8 characters", color=red)
        elif not self.verify_password_editor.text_valid:
            self.create_status_label.value = Status("Passwords do not match", color=red)
        elif not self.email_address_editor.text_valid:
            self.create_status_label.value = Status("E-mail address should be specified as user@domain", color=red)
        else:
            self.create_status_label.value = None
        # enable the accept button if everything is valid in the current panel
        if self.panel_view.currentWidget() is self.add_account_panel:
            inputs = [self.display_name_editor, self.sip_address_editor, self.password_editor]
        else:
            inputs = [self.name_editor, self.username_editor, self.new_password_editor, self.verify_password_editor, self.email_address_editor]
        self.accept_button.setEnabled(all(input.text_valid for input in inputs))

    def _initialize(self):
        self.display_name = user_info.fullname
        self.username = user_info.username.lower().replace(' ', '.')
        self.sip_address = u''
        self.password = u''
        self.verify_password = u''
        self.email_address = u''

    @run_in_auxiliary_thread
    def _create_sip_account(self, username, password, email_address, display_name, timezone=None):
        red = '#cc0000'
        if timezone is None and sys.platform != 'win32':
            try:
                timezone = open('/etc/timezone').read().strip()
            except (OSError, IOError):
                try:
                    timezone = '/'.join(os.readlink('/etc/localtime').split('/')[-2:])
                except (OSError, IOError):
                    pass
        enrollment_data = dict(username=username.lower().encode('utf-8'),
                               password=password.encode('utf-8'),
                               email=email_address.encode('utf-8'),
                               display_name=display_name.encode('utf-8'),
                               tzinfo=timezone)
        try:
            settings = SIPSimpleSettings()
            response = urllib2.urlopen(settings.server.enrollment_url, urllib.urlencode(dict(enrollment_data)))
            response_data = cjson.decode(response.read().replace(r'\/', '/'))
            response_data = defaultdict(lambda: None, response_data)
            if response_data['success']:
                from blink import Blink
                try:
                    certificate_path = None
                    passport = response_data['passport']
                    if passport is not None:
                        certificate_path = Blink().save_certificates(response_data['sip_address'], passport['crt'], passport['key'], passport['ca'])
                except (GNUTLSError, IOError, OSError):
                    pass
                account_manager = AccountManager()
                try:
                    account = Account(response_data['sip_address'])
                except AccountExists:
                    account = account_manager.get_account(response_data['sip_address'])
                account.enabled = True
                account.display_name = display_name
                account.auth.password = password
                account.sip.outbound_proxy = response_data['outbound_proxy']
                account.nat_traversal.msrp_relay = response_data['msrp_relay']
                account.xcap.xcap_root = response_data['xcap_root']
                account.tls.certificate = certificate_path
                account.server.settings_url = response_data['settings_url']
                account.save()
                account_manager.default_account = account
                call_in_gui_thread(self.accept)
            elif response_data['error'] == 'user_exists':
                call_in_gui_thread(self.username_editor.addException, username)
            else:
                call_in_gui_thread(setattr, self.create_status_label, 'value', Status(response_data['error_message'], color=red))
        except (cjson.DecodeError, KeyError):
            call_in_gui_thread(setattr, self.create_status_label, 'value', Status('Illegal server response', color=red))
        except urllib2.URLError, e:
            call_in_gui_thread(setattr, self.create_status_label, 'value', Status('Failed to contact server: %s' % e.reason, color=red))
        finally:
            call_in_gui_thread(self.setEnabled, True)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        self.sip_address_editor.addException(notification.data.account.id)

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        self.sip_address_editor.removeException(notification.data.account.id)

    def open_for_add(self):
        self.add_account_button.click()
        self.add_account_button.setFocus()
        self.accept_button.setEnabled(False)
        self._initialize()
        self.show()

    def open_for_create(self):
        self.create_account_button.click()
        self.create_account_button.setFocus()
        self.accept_button.setEnabled(False)
        self._initialize()
        self.show()

del ui_class, base_class


