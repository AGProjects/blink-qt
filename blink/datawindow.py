"""The Data window: the message history in bulk.

Messages: drill into an account's stored rows by type (category, location
sharing, or content type), action (location_start/update/stop, the action of a
metadata sidecar, ...), contact and year/month/day, look at any one of them,
and remove a selection here and from the journal on the server
(blink.data_manage). Import and export of data come as tabs of their own.

Three threads: the GUI; 'db', where the history is read and erased; and
'data-manage', which talks to the server and waits for the erasing, one batch
after the other, so a removal never runs ahead of the server's answer.
"""

import json
import threading
import uuid

from collections import Counter
import time

from datetime import datetime, timezone
from functools import partial

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
                             QPushButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from blink.contact_mangler import mangled_account_label, mangled_text, mangled_uri
from blink.data_manage import (with_related, CATEGORY_LABELS, ENCRYPTED, bulk_delete_payload, REMOVE_BATCH, JournalRemoveError, JournalRemoveRefused, JournalRemoveUnsupported, action_label,
                               aggregate_days, build_rows, count_by, remove_from_journal, row_label, select, type_label)
from blink.history import MessageHistory
from blink.message_envelopes import this_device_id
from blink.messages import MessageManager
from blink.logging import ActivityLog
from blink.util import run_in_gui_thread, translate


__all__ = ['DataWindow']


MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

PREVIEW_ROWS = 300      # rows of the selection listed, newest first


def _month_label(month):
    try:
        return '%s %s' % (MONTHS[int(month[5:7]) - 1], month[:4])
    except (ValueError, IndexError):
        return month


def _day_label(day):
    try:
        return '%d %s' % (int(day[8:10]), MONTHS[int(day[5:7]) - 1])
    except (ValueError, IndexError):
        return day


def _local_time(timestamp):
    """A stored timestamp (naive UTC) as local time."""
    try:
        value = datetime.fromisoformat(str(timestamp)).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return str(timestamp)[:19]
    return value.strftime('%Y-%m-%d %H:%M:%S')


# The fields of a row shown above its content, in this order, when they have a value
DETAIL_FIELDS = ('id', 'message_id', 'account_id', 'remote_uri', 'display_name', 'uri', 'timestamp', 'direction', 'content_type', 'category',
                 'related_action', 'related_msg_id', 'state', 'encryption_type', 'decrypted', 'decryption_error', 'disposition', 'read', 'has_link',
                 'journal_id', 'sip_callid', 'media_type', 'cpim_from', 'cpim_to', 'cpim_timestamp', 'private', 'expire_time')


def _pretty(value):
    """A body as it reads best: JSON indented, anything else as it is."""
    if value is None:
        return ''
    text = value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value)
    stripped = text.strip()
    if stripped[:1] in ('{', '['):
        try:
            return json.dumps(json.loads(stripped), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return text


def _journal_of(account_id):
    """(history url, token) of an account whose journal can be compacted, or None."""
    manager = AccountManager()
    if not manager.has_account(account_id):
        return None
    account = manager.get_account(account_id)
    if account is BonjourAccount() or not account.enabled:
        return None
    url, token = account.sms.history_synchronization_url, account.sms.history_synchronization_token
    if not url or not token:
        return None
    return str(url), str(token)


def _replicating_account(account_id):
    """The account, when its other devices can be told about a deletion: a SylkServer account (it has a
    journal) that replicates messages. Bonjour and other accounts only delete on this computer."""
    if _journal_of(account_id) is None:
        return None
    account = AccountManager().get_account(account_id)
    if not account.sms.enable_message_replication:
        return None
    return account


def _log(text):
    ActivityLog().info(f'[data] {text}')


class ManagePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.accounts = []          # [(account id, rows)]
        self.account_id = None
        self.rows = []              # data_manage.Row of the account
        self.kind = None
        self.action = None
        self.contact = None
        self.year = self.month = self.day = None
        self.loading = False
        self.busy = False
        self.cancelled = False
        self.progress = None        # (done, total) while removing
        self.phase = None           # what the removal is doing now, shown as the status
        self.result = None          # what the last removal did
        self.error = None
        self.seq = 0
        self._shown_row = None      # the row whose content the detail shows
        self.use_server = True      # the user's choice, applied when the account has a journal
        self.use_devices = True     # the user's choice, applied when the account replicates to other devices
        self.announce_note = None   # why the last deletion was not sent to the other devices
        self._build()
        self.refresh_controls()

    # -- the panel ---------------------------------------------------------------

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self.account_box = QComboBox(self)
        self.account_box.activated.connect(self._SH_AccountChanged)
        form.addRow(translate('data_window', 'Account'), self.account_box)

        self.kind_box = QComboBox(self)
        self.kind_box.setMaxVisibleItems(30)
        self.kind_box.activated.connect(self._SH_KindChanged)
        form.addRow(translate('data_window', 'Type'), self.kind_box)

        self.action_box = QComboBox(self)
        self.action_box.setMaxVisibleItems(30)
        self.action_box.activated.connect(self._SH_ActionChanged)
        form.addRow(translate('data_window', 'Action'), self.action_box)

        self.contact_box = QComboBox(self)
        self.contact_box.setMaxVisibleItems(30)
        self.contact_box.activated.connect(self._SH_ContactChanged)
        form.addRow(translate('data_window', 'Contact'), self.contact_box)

        period = QHBoxLayout()
        self.year_box = QComboBox(self)
        self.month_box = QComboBox(self)
        self.day_box = QComboBox(self)
        for box, slot in ((self.year_box, self._SH_YearChanged), (self.month_box, self._SH_MonthChanged), (self.day_box, self._SH_DayChanged)):
            box.setMaxVisibleItems(32)
            box.activated.connect(slot)
            period.addWidget(box, 1)
        form.addRow(translate('data_window', 'Period'), period)

        self.headline = QLabel(self)
        self.headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.headline.setWordWrap(True)
        font = QFont(self.headline.font())
        font.setBold(True)
        self.headline.setFont(font)
        layout.addWidget(self.headline)

        self.preview = QTreeWidget(self)
        self.preview.setRootIsDecorated(False)
        self.preview.setUniformRowHeights(True)
        self.preview.setAlternatingRowColors(True)
        self.preview.setHeaderLabels([translate('data_window', 'Time'), translate('data_window', 'Direction'), translate('data_window', 'Contact'),
                                      translate('data_window', 'Type'), translate('data_window', 'Content')])
        header = self.preview.header()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.preview.currentItemChanged.connect(self._SH_PreviewItemChanged)

        self.detail = QPlainTextEdit(self)
        self.detail.setReadOnly(True)
        self.detail.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.detail.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.detail.setPlaceholderText(translate('data_window', 'Click a message to see it in full'))

        self.message_delete_button = QPushButton(translate('data_window', 'Delete This Message'), self)
        self.message_delete_button.setToolTip(translate('data_window', 'Delete the message shown as in the conversation: here, on the server and on your other devices'))
        self.message_delete_button.clicked.connect(self._SH_MessageDeleteClicked)
        self.message_delete_button.setVisible(False)
        detail_box = QWidget(self)
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.detail, 1)
        detail_buttons = QHBoxLayout()
        detail_buttons.addStretch(1)
        detail_buttons.addWidget(self.message_delete_button)
        detail_layout.addLayout(detail_buttons)

        self.splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.splitter.addWidget(self.preview)
        self.splitter.addWidget(detail_box)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        layout.addWidget(self.splitter, 1)

        self.server_box = QCheckBox(translate('data_window', 'Also remove them from the journal on the server'), self)
        self.server_box.setChecked(True)
        self.server_box.clicked.connect(self._SH_ServerClicked)
        self.server_box.setToolTip(translate('data_window', 'A new device then no longer downloads them. Other devices keep the copies they have.'))
        layout.addWidget(self.server_box)

        self.devices_box = QCheckBox(translate('data_window', 'Also delete them from my other devices'), self)
        self.devices_box.setChecked(True)
        self.devices_box.clicked.connect(self._SH_DevicesClicked)
        layout.addWidget(self.devices_box)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton(translate('data_window', 'Refresh'), self)
        self.refresh_button.clicked.connect(lambda: self.refresh())
        buttons.addWidget(self.refresh_button)
        self.reset_button = QPushButton(translate('data_window', 'Reset filters'), self)
        self.reset_button.clicked.connect(self._SH_ResetClicked)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        self.stop_button = QPushButton(translate('data_window', 'Stop'), self)
        self.stop_button.clicked.connect(self._SH_StopClicked)
        buttons.addWidget(self.stop_button)
        self.delete_button = QPushButton(self)
        self.delete_button.clicked.connect(self._SH_DeleteClicked)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)

    @staticmethod
    def _fill(box, items, selected, separator_after=None):
        """items: [(value, title)]; a separator after the item at index `separator_after`."""
        box.blockSignals(True)
        box.clear()
        index = 0
        for position, (value, title) in enumerate(items):
            box.addItem(title, value)
            if value == selected:
                index = box.count() - 1
            if separator_after is not None and position == separator_after:
                box.insertSeparator(box.count())
        if box.count():
            box.setCurrentIndex(index)
        box.blockSignals(False)

    # -- what is selected ----------------------------------------------------------

    def period(self):
        return self.day or self.month or self.year

    def _period_label(self):
        if self.day:
            return '%s %s' % (_day_label(self.day), self.day[:4])
        if self.month:
            return _month_label(self.month)
        if self.year:
            return self.year
        return translate('data_window', 'All time')

    def selection(self):
        return select(self.rows, self.kind, self.action, self.contact, self.period())

    def _selection_label(self):
        label = type_label(self.kind)
        if self.action is not None:
            label += f' · {action_label(self.action)}'
        return label

    def refresh_controls(self):
        idle = not self.busy and not self.loading

        self._fill(self.account_box, [(account_id, f'{mangled_account_label(account_id)}  ({count})') for account_id, count in self.accounts], self.account_id)
        self.account_box.setEnabled(idle and len(self.accounts) > 1)

        # Each filter lists what is left once every OTHER filter is applied, so they narrow each
        # other in whatever order they are chosen: a contact first shows its types, a type first
        # shows its contacts. Year, month and day stay a chain of their own.
        period = self.period()
        rows_for_type = select(self.rows, contact=self.contact, period=period)
        kinds = count_by(rows_for_type, 'type')
        categorized = [(kind, count) for kind, count in kinds if kind in CATEGORY_LABELS and kind != 'location']
        others = [(kind, count) for kind, count in kinds if kind not in CATEGORY_LABELS or kind == 'location']
        items = [(None, translate('data_window', 'All types') + f'  ({len(rows_for_type)})')]
        items += [(kind, f'{type_label(kind)}  ({count})') for kind, count in categorized + others]
        self._fill(self.kind_box, items, self.kind, separator_after=len(categorized) if categorized and others else None)
        self.kind_box.setEnabled(idle and bool(kinds))

        # the discriminator under a type: only location sharing and the uncategorized types have one
        actions = count_by(select(self.rows, self.kind, contact=self.contact, period=period), 'action') if self.kind is not None else []
        if all(action == '' for action, count in actions):
            actions = []
        items = [(None, translate('data_window', 'All actions') + (f'  ({len(actions)})' if actions else ''))]
        items += [(action, f'{action_label(action)}  ({count})') for action, count in actions]
        self._fill(self.action_box, items, self.action)
        self.action_box.setEnabled(idle and bool(actions))

        contacts = count_by(select(self.rows, self.kind, self.action, period=period), 'contact')
        items = [(None, translate('data_window', 'All contacts') + f'  ({len(contacts)})')]
        items += [(contact, f'{mangled_uri(contact) or translate("data_window", "(no contact)")}  ({count})') for contact, count in contacts]
        self._fill(self.contact_box, items, self.contact)
        self.contact_box.setEnabled(idle and len(contacts) > 0)

        scoped = select(self.rows, self.kind, self.action, self.contact)
        items = [(None, translate('data_window', 'All years'))] + [(year, f'{year}  ({count})') for year, count in aggregate_days(scoped, 4)]
        self._fill(self.year_box, items, self.year)
        self.year_box.setEnabled(idle and len(items) > 1)

        items = [(None, translate('data_window', 'All months'))]
        if self.year:
            items += [(month, f'{_month_label(month)}  ({count})') for month, count in aggregate_days(scoped, 7, self.year)]
        self._fill(self.month_box, items, self.month)
        self.month_box.setEnabled(idle and bool(self.year))

        items = [(None, translate('data_window', 'All days'))]
        if self.month:
            items += [(day, f'{_day_label(day)}  ({count})') for day, count in aggregate_days(scoped, 10, self.month)]
        self._fill(self.day_box, items, self.day)
        self.day_box.setEnabled(idle and bool(self.month))

        selected = self.selection()
        line = f'{self._selection_label()} · {self._period_label()}'
        if self.contact:
            line += f' · {mangled_uri(self.contact)}'
        if self.loading:
            detail = translate('data_window', 'Reading the history…')
        elif not self.accounts:
            detail = translate('data_window', 'There is no message history')
        else:
            incoming = sum(1 for row in selected if row.direction == 'incoming')
            detail = translate('data_window', '%d messages: %d incoming, %d outgoing') % (len(selected), incoming, len(selected) - incoming)
        self.headline.setText(f'{line}\n{detail}' if self.account_id else detail)

        self._fill_preview(selected if not self.loading else [])

        journal = _journal_of(self.account_id) if self.account_id else None
        devices = _replicating_account(self.account_id) is not None if self.account_id else False
        self.devices_box.setEnabled(idle and devices)
        self.devices_box.setChecked(self.use_devices and devices)
        self.server_box.setEnabled(idle and journal is not None)
        self.server_box.setChecked(self.use_server and journal is not None)
        self.server_box.setText(translate('data_window', 'Also remove them from the journal on the server') if journal is not None
                                else translate('data_window', 'Deleted on this computer only: this account has no SylkServer journal (Bonjour or another server)'))

        if self.busy and self.progress:
            done, total = self.progress
            self.progress_bar.setVisible(True)
            if done == 0 and self.phase is not None:
                self.progress_bar.setRange(0, 0)        # busy, until the first batch is done
            else:
                self.progress_bar.setRange(0, max(total, 1))
                self.progress_bar.setValue(done)
        else:
            self.progress_bar.setVisible(False)

        if self.error:
            status, color = (f'{self.result}\n{self.error}' if self.result else self.error), '#e53935'
        elif self.busy:
            if self.cancelled:
                status = translate('data_window', 'Stopping after this batch…')
            elif self.phase:
                status = self.phase
            else:
                status = translate('data_window', 'Removing %d of %d…') % self.progress if self.progress else ''
            color = None
        elif self.result:
            status, color = self.result, None
        else:
            status, color = '', None
        self.status.setText(status)
        self.status.setStyleSheet(f'color: {color};' if color else '')

        self.refresh_button.setEnabled(idle)
        self.reset_button.setEnabled(idle and any(value is not None for value in (self.kind, self.action, self.contact, self.year)))
        self.stop_button.setVisible(self.busy)
        self.stop_button.setEnabled(self.busy and not self.cancelled)
        # a bulk tool: Delete takes the whole selection the filters make, never rows picked in the list
        # a bulk delete needs a type, a contact and a period
        scoped = self.kind is not None and self.contact is not None and bool(self.period())
        self.delete_button.setText(translate('data_window', 'Delete %d…') % len(selected) if selected else translate('data_window', 'Delete'))
        self.delete_button.setEnabled(idle and bool(selected) and scoped)
        self.delete_button.setToolTip('' if scoped else translate('data_window', 'Choose a type, a contact and a period first'))

    def _fill_preview(self, selected):
        self.preview.blockSignals(True)
        self.preview.clear()
        self.preview.blockSignals(False)
        self.detail.clear()
        self._shown_row = None
        self.message_delete_button.setVisible(False)
        newest = sorted(selected, key=lambda row: (row.timestamp, row.id), reverse=True)[:PREVIEW_ROWS]
        items = []
        for row in newest:
            item = QTreeWidgetItem([_local_time(row.timestamp), row.direction, mangled_uri(row.contact) or '', row_label(row),
                                    mangled_text(row.preview, uri=row.contact) if row.preview else ''])
            item.setData(0, Qt.ItemDataRole.UserRole, row.id)
            item.setToolTip(4, row.message_id or translate('data_window', '(no message id: kept on this computer only)'))
            items.append(item)
        self.preview.addTopLevelItems(items)
        if len(selected) > PREVIEW_ROWS:
            more = QTreeWidgetItem(['', '', '', '', translate('data_window', '… and %d older') % (len(selected) - PREVIEW_ROWS)])
            more.setFlags(Qt.ItemFlag.NoItemFlags)
            self.preview.addTopLevelItem(more)

    def _SH_MessageDeleteClicked(self):
        """The message shown, deleted the way the conversation deletes one: hidden here (with what is filed
        against it and its downloaded file) and one application/sylk-api-message-remove, never a bulk delete."""
        if self.busy or self.loading or self._shown_row is None:
            return
        row = next((row for row in self.rows if row.id == self._shown_row), None)
        if row is None:
            return
        from blink.history import HistoryManager
        if row.message_id:
            MessageHistory().tombstone_message(row.message_id, account_id=self.account_id, remote_uri=row.contact, source='deleted in the Data manager')
            HistoryManager().download_history.remove(row.message_id)
        else:
            MessageHistory().purge_messages(self.account_id, [row.id])
        sent = False
        manager = AccountManager()
        account = manager.get_account(self.account_id) if manager.has_account(self.account_id) else None
        if account is not None and account is not BonjourAccount() and account.enabled and row.message_id and row.contact:
            sent = MessageManager().send_message_remove(account, row.contact, row.message_id)
        _log(f'Deleted message {row.message_id or row.id} ({row_label(row)}) with {row.contact} of {self.account_id}' + (', removal sent to the server' if sent else ', here only'))
        self.result = translate('data_window', 'Deleted 1 message') + (translate('data_window', '; removal sent to the server and your other devices') if sent else '')
        self.error = None
        self.refresh(keep_status=True)

    # -- loading -------------------------------------------------------------------

    def refresh(self, keep_status=False):
        if self.busy:
            return
        self.loading = True
        if not keep_status:
            self.error = self.result = None
        self.refresh_controls()
        MessageHistory().data_accounts(self._got_accounts)

    @run_in_gui_thread
    def _got_accounts(self, accounts):
        self.accounts = accounts
        ids = [account_id for account_id, count in accounts]
        if self.account_id not in ids:
            self.account_id = ids[0] if ids else None
            self.kind = self.action = self.contact = self.year = self.month = self.day = None
        if self.account_id is None:
            self.rows = []
            self.loading = False
            self.refresh_controls()
            return
        self._load(self.account_id)

    def _load(self, account_id):
        self.seq += 1
        self.loading = True
        self.refresh_controls()
        MessageHistory().data_inventory(account_id, partial(self._inventory_read, self.seq, account_id))

    def _inventory_read(self, seq, account_id, db_rows):
        # in the db thread: classified there, not in the GUI
        self._got_inventory(seq, account_id, build_rows(db_rows))

    @run_in_gui_thread
    def _got_inventory(self, seq, account_id, rows):
        if seq != self.seq:
            return
        self.rows = rows
        self.loading = False
        # keep what was chosen as long as there is something left in it
        if self.kind is not None and not select(rows, self.kind):
            self.kind = None
        if self.action is not None and not select(rows, self.kind, self.action):
            self.action = None
        if self.contact is not None and not select(rows, self.kind, self.action, self.contact):
            self.contact = None
        for name in ('day', 'month', 'year'):
            value = getattr(self, name)
            if value and not select(rows, self.kind, self.action, self.contact, value):
                setattr(self, name, None)
        self.refresh_controls()

    # -- the controls ----------------------------------------------------------------

    def _SH_AccountChanged(self, index):
        account_id = self.account_box.itemData(index)
        if account_id == self.account_id:
            return
        self.account_id = account_id
        self.kind = self.action = self.contact = self.year = self.month = self.day = None
        self.result = self.error = None
        self._load(account_id)

    def _SH_KindChanged(self, index):
        self.kind = self.kind_box.itemData(index)
        self.action = None          # an action belongs to its type
        self.result = None
        self.refresh_controls()

    def _SH_ActionChanged(self, index):
        self.action = self.action_box.itemData(index)
        self.result = None
        self.refresh_controls()

    def _SH_ContactChanged(self, index):
        self.contact = self.contact_box.itemData(index)
        self.result = None
        self.refresh_controls()

    def _SH_ResetClicked(self):
        self.kind = self.action = self.contact = self.year = self.month = self.day = None
        self.result = None
        self.refresh_controls()

    def _SH_YearChanged(self, index):
        self.year = self.year_box.itemData(index)
        self.month = self.day = None
        self.result = None
        self.refresh_controls()

    def _SH_MonthChanged(self, index):
        self.month = self.month_box.itemData(index)
        self.day = None
        self.result = None
        self.refresh_controls()

    def _SH_DayChanged(self, index):
        self.day = self.day_box.itemData(index)
        self.result = None
        self.refresh_controls()

    def _SH_PreviewItemChanged(self, current, previous):
        row_id = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        self._shown_row = row_id
        self.message_delete_button.setVisible(row_id is not None)
        self.message_delete_button.setEnabled(row_id is not None and not self.busy and not self.loading)
        if row_id is None:
            self.detail.clear()
            return
        MessageHistory().data_message(row_id, self._got_message)

    @run_in_gui_thread
    def _got_message(self, row):
        if row is None:
            if self._shown_row is not None:
                self.detail.setPlainText(translate('data_window', 'This message is no longer stored.'))
            return
        if row['id'] != self._shown_row:
            return      # another one was clicked meanwhile
        lines = [f'{name}: {row[name]}' for name in DETAIL_FIELDS if row.get(name) not in (None, '')]
        if row.get('metadata'):
            lines += ['', '-- metadata --', _pretty(row['metadata'])]
        lines += ['', '-- content --', _pretty(row.get('content'))]
        self.detail.setPlainText(mangled_text('\n'.join(lines), uri=row.get('remote_uri')))

    def _SH_ServerClicked(self, checked):
        self.use_server = checked

    def _SH_DevicesClicked(self, checked):
        self.use_devices = checked

    def _SH_StopClicked(self):
        self.cancelled = True
        self.refresh_controls()

    def _SH_DeleteClicked(self):
        selected = self.selection()
        if not selected or self.busy or self.loading or self.kind is None or self.contact is None or not self.period():
            return
        journal = _journal_of(self.account_id) if self.server_box.isChecked() else None
        # what is filed against them goes with them: the ticks of a location share, the sidecars of a message
        targets = with_related(self.rows, selected)
        related = len(targets) - len(selected)
        # the criteria for the other devices of the account, each erasing what matches in its own history
        announce = None
        self.announce_note = None
        if self.devices_box.isChecked() and _replicating_account(self.account_id) is not None:
            announce = bulk_delete_payload(self.kind, self.action, self.contact, self.period(), this_device_id())
        self.busy = True
        self.cancelled = False
        self.error = self.result = None
        self.progress = (0, len(targets))
        self.refresh_controls()
        what = f'{self._selection_label()}, {self.period()}, {self.contact or "all contacts"}'
        _log(f'Deleting {len(selected)} messages' + (f' and {related} related' if related else '') + f' of {self.account_id} ({what})'
             + (' here and from the journal on the server' if journal else ' here only'))
        # the other devices are told first: whatever happens on the server next
        announced = False
        if announce is not None:
            account = _replicating_account(self.account_id)
            announced = account is not None and MessageManager().send_messages_bulk_delete(account, announce)
        # one id for the whole deletion: the bulk delete message and every /remove request carry it
        request_id = announce['bulk_delete_id'] if announce is not None else str(uuid.uuid4())
        self._delete(self.account_id, targets, journal, announced, request_id)

    # -- the removal -------------------------------------------------------------------

    @run_in_thread('data-manage')
    def _delete(self, account_id, rows, journal, announced=False, request_id=None):
        verify = SIPSimpleSettings().tls.verify_server
        erased = on_server = not_on_server = 0
        error = None
        batches = (len(rows) + REMOVE_BATCH - 1) // REMOVE_BATCH
        for number, start in enumerate(range(0, len(rows), REMOVE_BATCH), 1):
            if self.cancelled:
                break
            batch = rows[start:start + REMOVE_BATCH]
            of = f' (batch {number} of {batches})' if batches > 1 else ''
            if journal is not None:
                ids = sorted({row.message_id for row in batch if row.message_id})
                self._phase(translate('data_window', 'Removing %d from the journal on the server…') % len(ids) + of)
                started = time.monotonic()
                try:
                    removed = remove_from_journal(journal[0], journal[1], ids, verify=verify, request_id=request_id,
                                                  progress=partial(self._server_progress, start, len(batch), len(rows), of))
                except JournalRemoveUnsupported as e:
                    error = translate('data_window', 'The server cannot remove messages in bulk (SylkServer needs updating). Nothing more was deleted.')
                    _log(f'Deleting stopped: {e}')
                    break
                except JournalRemoveRefused as e:
                    error = translate('data_window', 'The server refused the API token of the account. Nothing more was deleted; try again once the account has a new token.')
                    _log(f'Deleting stopped: {e}')
                    break
                except JournalRemoveError as e:
                    error = translate('data_window', 'Removing from the server failed: %s. Nothing more was deleted.') % e
                    _log(f'Deleting stopped: {e}')
                    break
                on_server += len(removed)
                not_on_server += len(ids) - len(removed)
                _log(f'The server removed {len(removed)} of {len(ids)} from the journal of {account_id}{of} in {time.monotonic() - started:.1f}s')
            self._phase(translate('data_window', 'Erasing %d on this computer…') % len(batch) + of)
            erased += self._erase(account_id, [row.id for row in batch])
            self._progress(min(start + len(batch), len(rows)), len(rows))
        _log(f'Deleted {erased} of {len(rows)} messages of {account_id} (request {request_id})' + (f', {on_server} removed from the journal, {not_on_server} were not in it' if journal else '')
             + (' (stopped)' if self.cancelled and erased < len(rows) else ''))
        self._finished(account_id, erased, len(rows), on_server if journal else None, not_on_server, error, announced)

    @staticmethod
    def _erase(account_id, row_ids):
        """Erase on the db thread and wait for it: the next batch goes to the server only after this one is gone here."""
        done = threading.Event()
        result = []

        def finished(count):
            result.append(count)
            done.set()
        MessageHistory().purge_messages(account_id, row_ids, callback=finished)
        done.wait(600)
        return result[0] if result else 0

    @run_in_gui_thread
    def _phase(self, text):
        self.phase = text
        self.refresh_controls()

    @run_in_gui_thread
    def _server_progress(self, start, batch_size, total, of, done, removed, requested):
        """The server's report while it removes a batch: the bar and the status only, not the whole panel."""
        if not self.busy:
            return
        self.progress = (start + (done * batch_size // requested if requested else 0), total)
        self.phase = translate('data_window', 'Removing from the journal on the server: %d of %d done, %d removed') % (done, requested, removed) + of
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(self.progress[0])
        if not self.cancelled:
            self.status.setText(self.phase)

    @run_in_gui_thread
    def _progress(self, done, total):
        self.progress = (done, total)
        self.refresh_controls()

    @run_in_gui_thread
    def _finished(self, account_id, erased, total, on_server, not_on_server, error, announced=False):
        self.busy = False
        self.progress = None
        self.phase = None
        result = translate('data_window', 'Deleted %d of %d') % (erased, total)
        if on_server is not None:
            result += translate('data_window', ', %d removed from the server journal') % on_server
            if not_on_server:
                result += translate('data_window', ' (%d were not in it)') % not_on_server
        if self.cancelled and erased < total:
            result += translate('data_window', ', stopped')
        self.cancelled = False
        if announced:
            result += translate('data_window', '; your other devices were asked to delete the same')
        elif self.announce_note:
            result += '; ' + self.announce_note
        self.result = result
        self.error = error
        self.refresh(keep_status=True)


class DataWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(translate('data_window', 'Data manager'))
        self.resize(820, 640)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.manage_panel = ManagePanel(self.tabs)
        self.tabs.addTab(self.manage_panel, translate('data_window', 'Messages'))
        layout.addWidget(self.tabs)
        geometry = QSettings().value('data_window/geometry')
        if geometry:
            self.restoreGeometry(geometry)

    def show(self):
        super().show()
        self.raise_()
        self.activateWindow()
        self.manage_panel.refresh()

    def closeEvent(self, event):
        QSettings().setValue('data_window/geometry', self.saveGeometry())
        super().closeEvent(event)
