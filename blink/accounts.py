# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['AccountModel', 'ActiveAccountModel', 'AccountSelector', 'AddAccountDialog', 'ServerToolsAccountModel', 'ServerToolsWindow']

import os
import re
import sys
import urllib
import urllib2
from collections import defaultdict

from PyQt4 import uic
from PyQt4.QtCore import Qt, QAbstractListModel, QModelIndex, QUrl
from PyQt4.QtGui  import QAction, QButtonGroup, QComboBox, QIcon, QMenu, QMovie, QPalette, QPixmap, QSortFilterProxyModel, QStyledItemDelegate
from PyQt4.QtNetwork import QNetworkAccessManager
from PyQt4.QtWebKit  import QWebView

import cjson
from application.notification import IObserver, NotificationCenter
from application.python import Null
from gnutls.errors import GNUTLSError
from zope.interface import implements

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.configuration import DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from sipsimple.util import user_info

from blink.resources import Resources
from blink.widgets.labels import Status
from blink.util import QSingleton, call_in_gui_thread, run_in_gui_thread


class AccountInfo(object):
    def __init__(self, account, icon=None):
        self.account = account
        self.icon = icon
        self.registration_state = None

    @property
    def name(self):
        return u'Bonjour' if self.account is BonjourAccount() else unicode(self.account.id)

    def __eq__(self, other):
        if isinstance(other, basestring):
            return self.name == other
        elif isinstance(other, (Account, BonjourAccount)):
            return self.account == other
        elif isinstance(other, AccountInfo):
            return self.account == other.account
        return False

    def __ne__(self, other):
        return not self.__eq__(other)


class AccountModel(QAbstractListModel):
    implements(IObserver)

    def __init__(self, parent=None):
        super(AccountModel, self).__init__(parent)
        self.accounts = []

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
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
        icon = None
        if account is BonjourAccount():
            pixmap = QPixmap()
            if pixmap.load(Resources.get('icons/bonjour.png')):
                pixmap = pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                icon = QIcon(pixmap)
        self.beginInsertRows(QModelIndex(), len(self.accounts), len(self.accounts))
        self.accounts.append(AccountInfo(account, icon))
        self.endInsertRows()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, (Account, BonjourAccount)):
            position = self.accounts.index(notification.sender)
            self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        position = self.accounts.index(notification.data.account)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.accounts[position]
        self.endRemoveRows()

    def _NH_SIPAccountWillRegister(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'started'
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'succeeded'
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'failed'
        self.dataChanged.emit(self.index(position), self.index(position))

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
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
        account_info = index.data(Qt.UserRole)
        if account_info.registration_state == 'succeeded':
            option.palette.setColor(QPalette.Text, Qt.black)
        else:
            option.palette.setColor(QPalette.Text, Qt.gray)
        super(AccountDelegate, self).paint(painter, option, index)


class AccountSelector(QComboBox):
    implements(IObserver)

    def __init__(self, parent=None):
        super(AccountSelector, self).__init__(parent)
        self.currentIndexChanged[int].connect(self._SH_SelectionChanged)
        self.model().dataChanged.connect(self._SH_DataChanged)
        self.view().setItemDelegate(AccountDelegate(self.view()))

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="SIPAccountManagerDidChangeDefaultAccount")
        notification_center.add_observer(self, name="SIPAccountManagerDidStart")

    def setModel(self, model):
        self.model().dataChanged.disconnect(self._SH_DataChanged)
        model.dataChanged.connect(self._SH_DataChanged)
        super(AccountSelector, self).setModel(model)

    def _SH_DataChanged(self, topLeft, bottomRight):
        index = self.currentIndex()
        if topLeft.row() <= index <= bottomRight.row():
            account_info = self.itemData(index)
            palette = self.palette()
            if account_info.registration_state == 'succeeded':
                palette.setColor(QPalette.Text, Qt.black)
            else:
                palette.setColor(QPalette.Text, Qt.gray)
            self.setPalette(palette)

    def _SH_SelectionChanged(self, index):
        if index == -1:
            return
        account_info = self.itemData(index)
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
        self.username_editor.regexp = re.compile('^\w(?<=[^0_])[\w.-]{4,31}(?<=[^_.-])$', re.IGNORECASE) # in order to enable unicode characters add re.UNICODE to flags
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
            return self.display_name_editor.text()
        else:
            return self.name_editor.text()

    def _set_display_name(self, value):
        self.display_name_editor.setText(value)
        self.name_editor.setText(value)

    def _get_username(self):
        return self.username_editor.text()

    def _set_username(self, value):
        self.username_editor.setText(value)

    def _get_sip_address(self):
        return self.sip_address_editor.text()

    def _set_sip_address(self, value):
        self.sip_address_editor.setText(value)

    def _get_password(self):
        if self.panel_view.currentWidget() is self.add_account_panel:
            return self.password_editor.text()
        else:
            return self.new_password_editor.text()

    def _set_password(self, value):
        self.password_editor.setText(value)
        self.new_password_editor.setText(value)

    def _get_verify_password(self):
        return self.verify_password_editor.text()

    def _set_verify_password(self, value):
        self.verify_password_editor.setText(value)

    def _get_email_address(self):
        return self.email_address_editor.text()

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
            account.display_name = self.display_name or None
            account.auth.password = self.password
            account.save()
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
        self.verify_password_editor.regexp = re.compile(u'^%s$' % re.escape(text))

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
        self.username = user_info.username.lower().replace(u' ', u'.')
        self.sip_address = u''
        self.password = u''
        self.verify_password = u''
        self.email_address = u''

    @run_in_thread('network-io')
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
                except DuplicateIDError:
                    account = account_manager.get_account(response_data['sip_address'])
                account.enabled = True
                account.display_name = display_name or None
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


# Account server tools
#

class ServerToolsAccountModel(QSortFilterProxyModel):
    def __init__(self, model, parent=None):
        super(ServerToolsAccountModel, self).__init__(parent)
        self.setSourceModel(model)
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row, source_parent):
        source_model = self.sourceModel()
        source_index = source_model.index(source_row, 0, source_parent)
        account_info = source_model.data(source_index, Qt.UserRole)
        return bool(account_info.account is not BonjourAccount() and account_info.account.enabled and account_info.account.server.settings_url)


class ServerToolsWebView(QWebView):
    implements(IObserver)

    def __init__(self, parent=None):
        super(ServerToolsWebView, self).__init__(parent)
        self.access_manager = Null
        self.authenticated = False
        self.account = None
        self.user_agent = 'blink'
        self.tab = None
        self.task = None
        self.last_error = None
        self.urlChanged.connect(self._SH_URLChanged)

    @property
    def query_items(self):
        all_items = ('user_agent', 'tab', 'task')
        return [(name, value) for name, value in self.__dict__.iteritems() if name in all_items and value is not None]

    def _get_account(self):
        return self.__dict__['account']

    def _set_account(self, account):
        notification_center = NotificationCenter()
        old_account = self.__dict__.get('account', Null)
        if account is old_account:
            return
        self.__dict__['account'] = account
        self.authenticated = False
        if old_account:
            notification_center.remove_observer(self, sender=old_account)
        if account:
            notification_center.add_observer(self, sender=account)
        self.access_manager.authenticationRequired.disconnect(self._SH_AuthenticationRequired)
        self.access_manager.finished.disconnect(self._SH_Finished)
        self.access_manager = QNetworkAccessManager(self)
        self.access_manager.authenticationRequired.connect(self._SH_AuthenticationRequired)
        self.access_manager.finished.connect(self._SH_Finished)
        self.page().setNetworkAccessManager(self.access_manager)

    account = property(_get_account, _set_account)
    del _get_account, _set_account

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if '__id__' in notification.data.modified or 'auth.password' in notification.data.modified:
            self.authenticated = False
            self.reload()

    def _SH_AuthenticationRequired(self, reply, auth):
        if self.account and not self.authenticated:
            auth.setUser(self.account.id)
            auth.setPassword(self.account.auth.password)
            self.authenticated = True
        else:
            # we were already authenticated, yet it asks for the auth again. this means our credentials are not good.
            # we do not provide credentials anymore in order to fail and not try indefinitely, but we also reset the
            # authenticated status so that we try again when the page is reloaded.
            self.authenticated = False

    def _SH_Finished(self, reply):
        if reply.error() != reply.NoError:
            self.last_error = reply.errorString()
        else:
            self.last_error = None

    def _SH_URLChanged(self, url):
        query_items = dict(url.queryItems())
        self.tab = query_items.get('tab') or self.tab
        self.task = query_items.get('task') or self.task

    def load_account_page(self, account, tab=None, task=None):
        self.tab = tab
        self.task = task
        self.account = account
        url = QUrl(account.server.settings_url)
        for name, value in self.query_items:
            url.addQueryItem(name, value)
        self.load(url)


ui_class, base_class = uic.loadUiType(Resources.get('server_tools.ui'))

class ServerToolsWindow(base_class, ui_class):
    __metaclass__ = QSingleton

    def __init__(self, model, parent=None):
        super(ServerToolsWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        self.spinner_movie = QMovie(Resources.get('icons/servertools-spinner.mng'))
        self.spinner_label.setMovie(self.spinner_movie)
        self.spinner_label.hide()
        self.progress_bar.hide()
        while self.tab_widget.count():
            self.tab_widget.removeTab(0) # remove the tab(s) added in designer
        self.tab_widget.tabBar().hide()
        self.account_button.setMenu(QMenu(self.account_button))
        self.setWindowTitle('Blink Server Tools')
        self.setWindowIconText('Server Tools')
        self.model = model
        self.tab_widget.addTab(ServerToolsWebView(self), '')
        font = self.account_label.font()
        font.setPointSizeF(self.account_label.fontInfo().pointSizeF() + 2)
        font.setFamily("Sans Serif")
        self.account_label.setFont(font)
        self.model.rowsInserted.connect(self._SH_ModelChanged)
        self.model.rowsRemoved.connect(self._SH_ModelChanged)
        self.account_button.menu().triggered.connect(self._SH_AccountButtonMenuTriggered)
        web_view = self.tab_widget.currentWidget()
        web_view.loadStarted.connect(self._SH_WebViewLoadStarted)
        web_view.loadFinished.connect(self._SH_WebViewLoadFinished)
        web_view.loadProgress.connect(self._SH_WebViewLoadProgress)

    def _SH_AccountButtonMenuTriggered(self, action):
        view = self.tab_widget.currentWidget()
        account = action.data()
        self.account_label.setText(account.id)
        self.tab_widget.setTabText(self.tab_widget.currentIndex(), account.id)
        view.load_account_page(account, tab=view.tab, task=view.task)

    def _SH_WebViewLoadStarted(self):
        self.spinner_label.setMovie(self.spinner_movie)
        self.spinner_label.show()
        self.spinner_movie.start()
        self.progress_bar.setValue(0)
        #self.progress_bar.show()

    def _SH_WebViewLoadFinished(self, load_ok):
        self.spinner_movie.stop()
        self.spinner_label.hide()
        self.progress_bar.hide()
        if not load_ok:
            web_view = self.tab_widget.currentWidget()
            icon_path = Resources.get('icons/invalid.png')
            error_message = web_view.last_error or 'Unknown error'
            html = """
            <html>
             <head>
              <style>
                .icon    { width: 64px; height: 64px; float: left; }
                .message { margin-left: 74px; line-height: 64px; vertical-align: middle; }
              </style>
             </head>
             <body>
              <img class="icon" src="file:%s" />
              <div class="message">Failed to load web page: <b>%s</b></div>
             </body>
            </html>
            """ % (icon_path, error_message)
            web_view.loadStarted.disconnect(self._SH_WebViewLoadStarted)
            web_view.loadFinished.disconnect(self._SH_WebViewLoadFinished)
            web_view.setHtml(html)
            web_view.loadStarted.connect(self._SH_WebViewLoadStarted)
            web_view.loadFinished.connect(self._SH_WebViewLoadFinished)

    def _SH_WebViewLoadProgress(self, percent):
        self.progress_bar.setValue(percent)

    def _SH_ModelChanged(self, parent_index, start, end):
        menu = self.account_button.menu()
        menu.clear()
        for row in xrange(self.model.rowCount()):
            account_info = self.model.data(self.model.index(row, 0), Qt.UserRole)
            action = QAction(account_info.name, self)
            action.setData(account_info.account)
            menu.addAction(action)

    def open_settings_page(self, account):
        view = self.tab_widget.currentWidget()
        account = account or view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        self.account_label.setText(account.id)
        self.tab_widget.setTabText(self.tab_widget.currentIndex(), account.id)
        view.load_account_page(account, tab='settings')
        self.show()

    def open_search_for_people_page(self, account):
        view = self.tab_widget.currentWidget()
        account = account or view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        self.account_label.setText(account.id)
        self.tab_widget.setTabText(self.tab_widget.currentIndex(), account.id)
        view.load_account_page(account, tab='contacts', task='directory')
        self.show()

    def open_history_page(self, account):
        view = self.tab_widget.currentWidget()
        account = account or view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        self.account_label.setText(account.id)
        self.tab_widget.setTabText(self.tab_widget.currentIndex(), account.id)
        view.load_account_page(account, tab='calls')
        self.show()

    def open_buy_pstn_access_page(self, account):
        view = self.tab_widget.currentWidget()
        account = account or view.account
        if account is None or account.server.settings_url is None:
            account = self.account_button.menu().actions()[0].data()
        self.account_label.setText(account.id)
        self.tab_widget.setTabText(self.tab_widget.currentIndex(), account.id)
        view.load_account_page(account, tab='payments')
        self.show()

del ui_class, base_class


