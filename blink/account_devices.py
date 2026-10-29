"""The Devices tab of the Data window: the devices that use an account, as SylkServer records them.

The server keeps one object per device in the account storage, namespace `devices`
(SylkServer docs/ACCOUNT-DEVICES.md): its id and name, the client and version, when and
from which address it was used first and last, and how (websocket, sip, https). The
server writes them; a client lists and reads them, and may DELETE one to forget a
device (it is recorded again, as new, the next time it is used).

The listing and the reads are done in the 'account-storage' thread, as the avatars'.
"""

import json

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                             QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.threading import run_in_thread

from blink.account_storage import AccountStorage, storage_url, valid_name
from blink.contact_mangler import mangled_account_label
from blink.logging import ActivityLog
from blink.message_envelopes import this_device_id
from blink.util import run_in_gui_thread, translate


__all__ = ['DevicesPanel', 'DEVICES_NAMESPACE']


DEVICES_NAMESPACE = 'devices'


def _tr(text):
    return translate('data_window', text)


def _when(milliseconds):
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def _name(device):
    """The user agent of the device, else the name it gave itself (recorded before clients sent one)."""
    return device.get('user_agent') or device.get('device_name') or ''


def _accounts():
    """The enabled accounts with a SylkServer account storage."""
    return [account for account in AccountManager().get_accounts()
            if isinstance(account, Account) and account is not BonjourAccount() and account.enabled and storage_url(account)]


class DevicesPanel(QWidget):
    columns = ('Id', 'User Agent', 'First used', 'Last used', 'Last address', 'Via')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.account_id = None
        self.devices = []           # the entries of account_id, as the server keeps them (plus 'object_id')
        self.loading = False
        self.seq = 0
        self._build()
        self.refresh_controls()

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.account_box = QComboBox(self)
        self.account_box.activated.connect(self._SH_AccountChanged)
        form.addRow(_tr('Account'), self.account_box)

        intro = QLabel(_tr('The devices that used this account, as the server records them: when and from which address each was used '
                           'first and last. Forget removes a device from the list; it is listed again, as new, the next time it is used.'), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.tree = QTreeWidget(splitter)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.setHeaderLabels([_tr(column) for column in self.columns])
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)     # the user resizes and moves them
        header.setSectionsMovable(True)
        header.setStretchLastSection(True)
        self._sized = False
        self.tree.currentItemChanged.connect(self._SH_CurrentItemChanged)
        self.detail = QPlainTextEdit(splitter)
        self.detail.setReadOnly(True)
        self.detail.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.forget_button = QPushButton(_tr('Forget'), self)
        self.forget_button.setToolTip(_tr('Remove the chosen device from the list on the server'))
        self.forget_button.clicked.connect(self._SH_ForgetClicked)
        buttons.addWidget(self.forget_button)
        self.refresh_button = QPushButton(_tr('Refresh'), self)
        self.refresh_button.clicked.connect(lambda: self.refresh())
        buttons.addWidget(self.refresh_button)
        layout.addLayout(buttons)

    # accounts

    def refresh_accounts(self):
        accounts = [str(account.id) for account in _accounts()]
        self.account_box.blockSignals(True)
        self.account_box.clear()
        for account_id in accounts:
            self.account_box.addItem(mangled_account_label(account_id), account_id)
        if self.account_id not in accounts:
            self.account_id = accounts[0] if accounts else None
        if self.account_id is not None:
            self.account_box.setCurrentIndex(self.account_box.findData(self.account_id))
        self.account_box.blockSignals(False)
        if self.account_id is None:
            self._show([], _tr('No account has a server that keeps its devices'))
        self.refresh_controls()

    def _SH_AccountChanged(self, index):
        account_id = self.account_box.itemData(index)
        if account_id != self.account_id:
            self.account_id = account_id
            self.refresh()

    def _account(self):
        manager = AccountManager()
        if self.account_id is None or not manager.has_account(self.account_id):
            return None
        return manager.get_account(self.account_id)

    # loading

    def refresh(self):
        self.refresh_accounts()
        account = self._account()
        if account is None:
            return
        self.seq += 1
        self.loading = True
        self.status.setText(_tr('Reading the devices from the server…'))
        self.refresh_controls()
        self._load(self.seq, account)

    @run_in_thread('account-storage')
    def _load(self, seq, account):
        storage = AccountStorage()
        response = storage._request(account, 'GET', DEVICES_NAMESPACE)
        if response.status == 404:
            self._loaded(seq, str(account.id), [], None)
            return
        if response.status != 200:
            self._loaded(seq, str(account.id), None, _tr('The server answered HTTP %s') % (response.status or _tr('error')) if response.status else _tr('The server cannot be reached'))
            return
        try:
            listing = response.json() or {}
        except Exception:
            listing = {}
        object_ids = [item.get('id') for item in listing.get('objects') or [] if isinstance(item, dict) and item.get('id')]
        devices = []
        for object_id in object_ids:
            if not valid_name(object_id):
                continue
            entry = storage._request(account, 'GET', f'{DEVICES_NAMESPACE}/{object_id}')
            if entry.status != 200:
                continue
            try:
                device = json.loads(entry.body.decode('utf-8'))
            except (ValueError, UnicodeDecodeError, AttributeError):
                continue
            if isinstance(device, dict):
                device['object_id'] = object_id
                devices.append(device)
        ActivityLog().info(f'[data] Devices of {account.id}: {len(devices)} listed by the server')
        self._loaded(seq, str(account.id), devices, None)

    @run_in_gui_thread
    def _loaded(self, seq, account_id, devices, error):
        if seq != self.seq or account_id != self.account_id:
            return
        self.loading = False
        if devices is None:
            self._show([], error)
        else:
            self._show(devices, _tr('No device recorded yet') if not devices else (_tr('1 device') if len(devices) == 1 else _tr('%d devices') % len(devices)))
        self.refresh_controls()

    def _show(self, devices, status):
        self.devices = devices
        mine = this_device_id()
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for device in devices:
            this_one = bool(mine) and device.get('device_id') == mine
            identifier = device.get('device_id') or device.get('object_id') or ''
            name = _name(device)
            if this_one:
                name = _tr('%s (this computer)') % (name or _tr('This computer'))
            item = QTreeWidgetItem(self.tree, [identifier, name, _when(device.get('first_seen')), _when(device.get('last_seen')),
                                               device.get('last_ip') or '', device.get('last_via') or ''])
            item.setData(0, Qt.ItemDataRole.UserRole, device)
            if this_one:
                for column in range(len(self.columns)):
                    font = item.font(column)
                    font.setBold(True)
                    item.setFont(column, font)
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(3, Qt.SortOrder.DescendingOrder)     # the latest used first
        if devices and not self._sized:
            # fitted to what is shown the first time, then left as the user drags them
            self._sized = True
            for column in range(len(self.columns) - 1):
                self.tree.resizeColumnToContents(column)
                self.tree.setColumnWidth(column, min(self.tree.columnWidth(column) + 12, 320))
        self.detail.clear()
        self.status.setText(status or '')

    def _current(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def _SH_CurrentItemChanged(self, current, previous):
        device = self._current()
        if device is None:
            self.detail.clear()
        else:
            row = {key: value for key, value in device.items() if key != 'object_id'}     # object_id is ours, the server row has it as id
            self.detail.setPlainText(json.dumps(row, indent=2, ensure_ascii=False))
        self.refresh_controls()

    def refresh_controls(self):
        device = self._current()
        this_one = device is not None and bool(this_device_id()) and device.get('device_id') == this_device_id()
        self.account_box.setEnabled(not self.loading)
        self.refresh_button.setEnabled(self.account_id is not None and not self.loading)
        self.forget_button.setEnabled(device is not None and not this_one and not self.loading)
        self.forget_button.setToolTip(_tr('This computer is listed again as soon as it uses the account') if this_one else _tr('Remove the chosen device from the list on the server'))

    # forgetting

    def _SH_ForgetClicked(self):
        device, account = self._current(), self._account()
        if device is None or account is None:
            return
        name = _name(device) or device.get('device_id') or device['object_id']
        question = _tr('Remove %s from the devices of the account on the server? It is listed again, as new, the next time it uses the account.') % name
        if QMessageBox.question(self, _tr('Forget device'), question) != QMessageBox.StandardButton.Yes:
            return
        self.loading = True
        self.status.setText(_tr('Removing %s…') % name)
        self.refresh_controls()
        self._forget(self.seq, account, device['object_id'], name)

    @run_in_thread('account-storage')
    def _forget(self, seq, account, object_id, name):
        response = AccountStorage()._request(account, 'DELETE', f'{DEVICES_NAMESPACE}/{object_id}')
        ok = response.status in (200, 204, 404)
        ActivityLog().info(f'[data] Devices of {account.id}: forgetting {name} ({object_id}): ' + ('done' if ok else f'HTTP {response.status or "error"}'))
        self._forgotten(seq, ok, name, response.status)

    @run_in_gui_thread
    def _forgotten(self, seq, ok, name, status):
        self.loading = False
        if not ok:
            self.status.setText(_tr('%s was not removed: the server answered HTTP %s') % (name, status or _tr('error')))
            self.refresh_controls()
            return
        self.refresh()
