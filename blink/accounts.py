# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['AccountModel', 'ActiveAccountModel', 'AccountSelector']

from PyQt4.QtCore import Qt, QAbstractListModel, QModelIndex
from PyQt4.QtGui  import QComboBox, QIcon, QPalette, QPixmap, QSortFilterProxyModel, QStyledItemDelegate

from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.account import Account, AccountManager, BonjourAccount

from blink.resources import Resources
from blink.util import run_in_gui_thread


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
