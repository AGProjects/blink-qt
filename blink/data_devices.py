"""The Import from device and Export to device tabs of the Data window.

Import from device: a Sylk Mobile (or another Blink) of the same account
exports its history and announces it (application/sylk-data-export); the Data
window comes forward on this tab, connected to it, and imports what is chosen,
add-only. Rows are stored the way journal entries are
(MessageManager.apply_imported_messages); files go into the transfer folders
the conversations use.

Export to device: this computer serves the account's history to its other
devices (blink.data_export) and announces it to them; their Import screen
does the rest. It runs until Stop or until the Data window is closed.

No database or network work happens in the GUI thread: 'data-import' talks to
the other device and reads the history, in order, so a preview never
overtakes an import; 'data-import-ping' checks the other device is still
there; 'data-export' reads the history and starts the server.
"""

import os
import socket
import threading
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QFormLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QStackedWidget,
                             QVBoxLayout, QWidget)

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from blink import data_export, data_import
from blink.contact_mangler import mangled_account_label, mangled_uri
from blink.history import MessageHistory
from blink.logging import ActivityLog
from blink.message_envelopes import file_transfer_envelope
from blink.messages import MessageManager
from blink.resources import ApplicationData
from blink.util import run_in_gui_thread, translate


__all__ = ['ImportPanel', 'ExportPanel']


MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

HEARTBEAT_MS = 4000
HEARTBEAT_SECONDS = HEARTBEAT_MS / 1000.0
# the other device is declared gone only when both hold: this many pings in a row unanswered,
# and nothing at all came back from it for this long (one missed ping is a device busy sealing a file)
PING_FAILURES_LOST = 3
SILENCE_LOST_SECONDS = 30.0
MESSAGE_BATCH = 500
FILE_BATCH = 20


def _tr(text):
    return translate('data_window', text)


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


def _size(size):
    from blink.messagepane.format import format_size
    return format_size(size) or '0 bytes'


def _wait(call):
    """Run a MessageHistory call that answers through a callback, from a worker thread, and return its answer."""
    done = threading.Event()
    result = []

    def callback(value):
        result.append(value)
        done.set()
    call(callback)
    done.wait(600)
    return result[0] if result else None


def _fill(box, items, selected):
    """items: [(value, title)]"""
    box.blockSignals(True)
    box.clear()
    index = 0
    for value, title in items:
        box.addItem(title, value)
        if value == selected:
            index = box.count() - 1
    if box.count():
        box.setCurrentIndex(index)
    box.blockSignals(False)


def _decrypt_file(account, payload):
    """A file's plaintext, with the account's private key."""
    import pgpy
    if account.sms.private_key is None or not os.path.exists(account.sms.private_key.normalized):
        raise ValueError('no private key for %s on this computer' % account.id)
    key, _ = pgpy.PGPKey.from_file(account.sms.private_key.normalized)
    blob = payload
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        text = None
    if text is not None and 'BEGIN PGP' in text:
        blob = text
    message = key.decrypt(pgpy.PGPMessage.from_blob(blob))
    literal = getattr(message, '_message', None)
    contents = getattr(literal, '_contents', None)      # the literal data's bytes as sent, whatever its format byte says
    if contents is None:
        contents = message.message
    return bytes(contents) if not isinstance(contents, str) else contents.encode('latin-1')


def _write_file(path, payload):
    from application.system import makedirs
    makedirs(os.path.dirname(path))
    temporary = '%s.download' % path
    with open(temporary, 'wb') as file:
        file.write(payload)
    os.replace(temporary, path)


def _log(text):
    ActivityLog().info(text)


class _ImportSession(object):
    """One import from one other device, for one account."""

    def __init__(self, account, announcement):
        self.account = account
        self.account_id = str(account.id)
        self.announcement = announcement
        self.client = data_import.ImportClient(announcement['server'], announcement['key'], announcement.get('enc') or '')
        self.closed = False
        self.disconnected = False
        self.inventory = None       # (message ids here, {message id: (peer, meta)} of transfers whose file is missing)

    def matches(self, account, announcement):
        return (self.account_id == str(account.id) and self.announcement.get('server') == announcement.get('server')
                and self.announcement.get('key') == announcement.get('key'))


class ImportPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self._reset_state()
        self._timer = QTimer(self)
        self._timer.setInterval(HEARTBEAT_MS)
        self._timer.timeout.connect(self._SH_Heartbeat)
        self._build()
        self.refresh_controls()

    def _reset_state(self):
        self.summary = None
        self.index = {}
        self.kind = 'messages'
        self.category = 'all'
        self.contact = None
        self.year = self.month = self.day = None
        self.new_items = []
        self.preview = None
        self.result = None
        self.busy = False
        self.cancelled = False
        self.error = None
        self.notice = None
        self.progress = None
        self.seq = 0
        self.ping_failures = 0
        self.pinging = False

    # -- the tab ------------------------------------------------------------------

    def _build(self):
        layout = QVBoxLayout(self)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)

        self.hint = QLabel(_tr('Import messages and files from Sylk Mobile, or from Blink on another computer.\n\n'
                               'Start the data export on that device, on the same network as this computer and with the same account. '
                               'Blink connects to it by itself and shows here what it can import.'), self)
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.hint)

        page = QWidget(self)
        box = QVBoxLayout(page)
        self.source = QLabel(page)
        self.source.setWordWrap(True)
        box.addWidget(self.source)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        box.addLayout(form)

        kinds = QHBoxLayout()
        self.kind_group = QButtonGroup(page)
        self.messages_button = QPushButton(_tr('Messages'), page)
        self.files_button = QPushButton(_tr('Files'), page)
        for index, button in enumerate((self.messages_button, self.files_button)):
            button.setCheckable(True)
            self.kind_group.addButton(button, index)
            kinds.addWidget(button)
        kinds.addStretch(1)
        self.kind_group.idClicked.connect(self._SH_KindClicked)
        form.addRow(_tr('Import'), kinds)

        self.category_box = QComboBox(page)
        self.category_box.activated.connect(self._SH_CategoryChanged)
        form.addRow(_tr('Category'), self.category_box)
        self.contact_box = QComboBox(page)
        self.contact_box.setMaxVisibleItems(30)
        self.contact_box.activated.connect(self._SH_ContactChanged)
        form.addRow(_tr('Contact'), self.contact_box)
        period = QHBoxLayout()
        self.year_box, self.month_box, self.day_box = QComboBox(page), QComboBox(page), QComboBox(page)
        for combo, slot in ((self.year_box, self._SH_YearChanged), (self.month_box, self._SH_MonthChanged), (self.day_box, self._SH_DayChanged)):
            combo.setMaxVisibleItems(32)
            combo.activated.connect(slot)
            period.addWidget(combo, 1)
        form.addRow(_tr('Period'), period)

        self.headline = QLabel(page)
        self.headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.headline.setWordWrap(True)
        font = QFont(self.headline.font())
        font.setBold(True)
        self.headline.setFont(font)
        box.addWidget(self.headline)
        self.progress_bar = QProgressBar(page)
        self.progress_bar.setVisible(False)
        box.addWidget(self.progress_bar)
        self.status = QLabel(page)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.status)
        box.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton(_tr('Disconnect'), page)
        self.close_button.clicked.connect(self._SH_CloseClicked)
        buttons.addWidget(self.close_button)
        self.import_button = QPushButton(_tr('Import'), page)
        self.import_button.clicked.connect(self._SH_ImportClicked)
        buttons.addWidget(self.import_button)
        box.addLayout(buttons)
        self.stack.addWidget(page)

    # -- sessions ---------------------------------------------------------------------

    def open(self, account, announcement):
        """A fresh announcement: connect to it, unless it is the one in place or an import is running."""
        current = self.session
        if current is not None:
            if current.matches(account, announcement):
                return
            if self.busy:
                _log(f'[import] An import is running; not switching to {announcement["server"]}')
                return
            self.close_session()
        self._reset_state()
        self.session = _ImportSession(account, announcement)
        self.session.client.on_retry = partial_retry(self)
        self.session.client.should_stop = lambda session=self.session: session.closed or session.disconnected
        self.refresh_controls()
        self._connect(self.session)

    def close_session(self):
        session, self.session = self.session, None
        if session is not None:
            session.closed = True
            self.cancelled = True
        self._timer.stop()
        self._reset_state()
        self.refresh_controls()

    def is_busy(self):
        return self.busy

    # -- what the tab shows ---------------------------------------------------------------

    def _remaining_contacts(self):
        counts = {}
        for item in self.new_items:
            contact = item.get('contact')
            if contact:
                counts[contact] = counts.get(contact, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def _new_days(self):
        return [item.get('day') or '' for item in self.new_items if not self.contact or item.get('contact') == self.contact]

    def period(self):
        return self.day or self.month or self.year or 'all'

    def _period_label(self):
        if self.day:
            return '%s %s' % (_day_label(self.day), self.day[:4])
        if self.month:
            return _month_label(self.month)
        if self.year:
            return self.year
        return _tr('All time')

    def refresh_controls(self):
        session = self.session
        self.stack.setCurrentIndex(1 if session is not None else 0)
        if session is None:
            return
        live = self.summary is not None and not session.disconnected
        idle = live and not self.busy

        device = (self.summary or {}).get('device') or {}
        source = device.get('useragent') or device.get('name') or session.client.server
        self.source.setText((_tr('From %s, for %s') % (source, mangled_account_label(session.account_id))) if self.summary is not None
                            else _tr('Connecting to %s…') % session.client.server)

        self.messages_button.setChecked(self.kind == 'messages')
        self.files_button.setChecked(self.kind == 'files')
        self.messages_button.setEnabled(idle)
        self.files_button.setEnabled(idle)

        categories = data_import.FILE_CATEGORIES if self.kind == 'files' else data_import.MESSAGE_CATEGORIES
        _fill(self.category_box, [(value, _tr(title)) for value, title in categories], self.category)
        self.category_box.setEnabled(idle)

        remaining = self._remaining_contacts()
        items = [(None, _tr('All contacts'))] + [(contact, f'{mangled_uri(contact)}  ({count})') for contact, count in remaining]
        if self.contact and self.contact not in [contact for contact, count in remaining]:
            items.append((self.contact, mangled_uri(self.contact)))
        _fill(self.contact_box, items, self.contact)
        self.contact_box.setEnabled(idle and len(items) > 1)

        days = data_import.day_list(self.index, self.kind, self.category)
        new_days = self._new_days()
        items = [(None, _tr('All years'))] + [(year, f'{year}  ({count})') for year, count in data_import.aggregate(days, 4)]
        _fill(self.year_box, items, self.year)
        self.year_box.setEnabled(idle and len(items) > 1)
        year_has_new = bool(self.year) and any(day.startswith(self.year) for day in new_days)
        items = [(None, _tr('All months'))]
        if year_has_new:
            items += [(month, f'{_month_label(month)}  ({count})') for month, count in data_import.aggregate(days, 7, self.year)]
        _fill(self.month_box, items, self.month)
        self.month_box.setEnabled(idle and year_has_new)
        month_has_new = bool(self.month) and any(day.startswith(self.month) for day in new_days)
        items = [(None, _tr('All days'))]
        if month_has_new:
            items += [(day, f'{_day_label(day)}  ({count})') for day, count in data_import.aggregate(days, 10, self.month)]
        _fill(self.day_box, items, self.day)
        self.day_box.setEnabled(idle and month_has_new)

        title = _tr(dict(categories).get(self.category, self.category))
        line = f'{title} · {self._period_label()}' + (f' · {mangled_uri(self.contact)}' if self.contact else '')
        if self.summary is None:
            detail = '' if self.error else _tr('Reading the other device…')
        elif self.result is not None:
            detail = (_tr('Cancelled · ') if self.result.get('cancelled') else '') + _tr('Added %d new · %d already here') % (
                self.result['imported'], self.result['server'] - self.result['new'])
            if self.result.get('files_failed'):
                detail += _tr(' · %d file(s) could not be copied') % self.result['files_failed']
        elif self.preview is None:
            detail = '…'
        elif self.preview['new']:
            detail = _tr('%d new of %d will be added') % (self.preview['new'], self.preview['server'])
        elif self.preview['server']:
            detail = _tr('All %d already imported') % self.preview['server']
        else:
            detail = _tr('Nothing here')
        self.headline.setText(f'{line}\n{detail}' if self.summary is not None else detail)

        if self.busy and self.progress:
            done, total = self.progress
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(done)
        else:
            self.progress_bar.setVisible(False)

        if session.disconnected:
            status, color = _tr('The other device stopped sharing: connection lost. Start the export again to continue; what was imported is kept.'), '#e53935'
        elif self.error:
            status, color = self.error, '#e53935'
        elif self.notice:
            status, color = self.notice, None
        elif self.busy and self.progress:
            status, color = '%d / %d' % self.progress, None
        else:
            status, color = '', None
        self.status.setText(status)
        self.status.setStyleSheet(f'color: {color};' if color else '')

        new = self.preview['new'] if self.preview is not None else 0
        if new:
            self.import_button.setText(_tr('Import %d') % new)
        elif self.preview is not None and self.preview['server']:
            self.import_button.setText(_tr('All imported'))
        else:
            self.import_button.setText(_tr('Nothing new'))
        self.import_button.setEnabled(idle and new > 0)
        self.import_button.setVisible(live)
        self.close_button.setText(_tr('Cancel') if self.busy else _tr('Disconnect'))

    # -- the controls ----------------------------------------------------------------------

    def _SH_KindClicked(self, index):
        kind = 'files' if index == 1 else 'messages'
        if kind == self.kind:
            return
        self.kind = kind
        self.category = 'all'
        self.contact = self.year = self.month = self.day = None
        self._selection_changed(new_items=True)

    def _SH_CategoryChanged(self, index):
        value = self.category_box.itemData(index) or 'all'
        if value == self.category:
            return
        self.category = value
        self.contact = self.year = self.month = self.day = None
        self._selection_changed(new_items=True)

    def _SH_ContactChanged(self, index):
        value = self.contact_box.itemData(index)
        if value == self.contact:
            return
        self.contact = value
        self.year = self.month = self.day = None
        self._load_calendar(self.session, self.contact)       # the calendar counts follow the contact, as on the phone
        self._selection_changed()

    def _SH_YearChanged(self, index):
        self.year = self.year_box.itemData(index)
        self.month = self.day = None
        self._selection_changed()

    def _SH_MonthChanged(self, index):
        self.month = self.month_box.itemData(index)
        self.day = None
        self._selection_changed()

    def _SH_DayChanged(self, index):
        self.day = self.day_box.itemData(index)
        self._selection_changed()

    def _SH_CloseClicked(self):
        if self.busy:
            self.cancelled = True
            self.status.setText(_tr('Stopping…'))
        else:
            self.close_session()

    def _SH_ImportClicked(self):
        if self.busy or not self.preview or not self.preview['new'] or self.session is None:
            return
        self.busy = True
        self.cancelled = False
        self.error = self.result = None
        self.progress = (0, self.preview['new'])
        self.refresh_controls()
        self._import(self.session, self.kind, self.category, self.period(), self.contact)

    def _SH_Heartbeat(self):
        session = self.session
        if session is None or session.disconnected or self.pinging or session.client.busy:
            return      # a request in flight is the other device working for us: a ping would only queue behind it
        if time.monotonic() - session.client.last_success < HEARTBEAT_SECONDS:
            self.ping_failures = 0
            return
        self.pinging = True
        self._ping(session)

    def _selection_changed(self, new_items=False):
        self.seq += 1
        self.preview = None
        self.result = None
        self.refresh_controls()
        if new_items:
            self._load_new_items(self.session, self.kind, self.category, self.seq)
        self._load_preview(self.session, self.kind, self.category, self.period(), self.contact, self.seq)

    # -- local state (worker thread) -----------------------------------------------------------

    @staticmethod
    def _inventory(session):
        if session.inventory is None:
            ids, transfers = _wait(lambda callback: MessageHistory().data_import_inventory(session.account_id, callback)) or (set(), [])
            storage = data_export.TransferStorage(ApplicationData.get('file_transfers'), ApplicationData.get('downloads'))
            missing = {}
            for message_id, peer, content in transfers:
                meta = file_transfer_envelope(content) if content else None
                if meta and meta.get('filename') and storage.find(meta, message_id) is None:
                    missing[message_id] = (peer, meta)
            session.inventory = (ids, missing)
            _log(f'[import] {session.account_id} holds {len(ids)} messages, {len(missing)} transfers without their file')
        return session.inventory

    def _local_ids(self, session, kind):
        """The ids that do not need importing; for files, a row whose file is missing is left out, so its bytes are fetched again."""
        ids, missing = self._inventory(session)
        return ids - set(missing) if kind == 'files' else ids

    # -- talking to the other device -----------------------------------------------------------

    @run_in_thread('data-import')
    def _connect(self, session):
        if session.closed:
            return
        try:
            summary = session.client.summary()
            index = session.client.calendar()
        except data_import.ExportServerError as e:
            _log(f'[import] Cannot reach {session.client.server}: {e}')
            self._connect_failed(session, str(e))
            return
        device = summary.get('device') or {}
        _log(f'[import] Connected to {session.client.server} ({device.get("useragent") or device.get("name")}) for {summary.get("account")}, encrypted={bool(session.client.enc_key)}')
        self._connected(session, summary, index)

    @run_in_gui_thread
    def _connect_failed(self, session, reason):
        if session is self.session:
            self.error = _tr('Could not reach %s') % session.client.server + '\n' + reason
            self.refresh_controls()

    @run_in_gui_thread
    def _connected(self, session, summary, index):
        if session is not self.session:
            return
        self.summary = summary
        self.index = index
        self._timer.start()
        self._selection_changed(new_items=True)

    @run_in_thread('data-import-ping')
    def _ping(self, session):
        try:
            if session.client.ping():
                if self.ping_failures:
                    _log(f'[import] {session.client.server} answers again after {self.ping_failures} missed pings')
                self.ping_failures = 0
                self._notice(session, None)
                return
            self.ping_failures += 1
            silence = time.monotonic() - session.client.last_success
            _log(f'[import] {session.client.server} did not answer ping {self.ping_failures}/{PING_FAILURES_LOST} (silent for {silence:.0f}s)')
            if self.ping_failures >= PING_FAILURES_LOST and silence >= SILENCE_LOST_SECONDS:
                self._lost(session)
            else:
                self._notice(session, _tr('The other device is not answering, retrying…'))
        finally:
            self.pinging = False

    @run_in_gui_thread
    def _notice(self, session, text):
        if session is self.session and self.notice != text:
            self.notice = text
            self.refresh_controls()

    @run_in_gui_thread
    def _lost(self, session):
        if session is not self.session or session.disconnected:
            return
        _log(f'[import] {session.client.server} stopped answering')
        self.notice = None
        session.disconnected = True
        self.cancelled = True
        self._timer.stop()
        self.refresh_controls()

    @run_in_thread('data-import')
    def _load_calendar(self, session, contact):
        if session is None or session.closed:
            return
        try:
            index = session.client.calendar(contact)
        except data_import.ExportServerError as e:
            _log(f'[import] Calendar failed: {e}')
            index = {}
        self._got_calendar(session, contact, index)

    @run_in_gui_thread
    def _got_calendar(self, session, contact, index):
        if session is self.session and contact == self.contact:
            self.index = index
            self.refresh_controls()

    @run_in_thread('data-import')
    def _load_new_items(self, session, kind, category, seq):
        """What is left to import for kind and category over every contact: the contact list and the calendar gating come from it."""
        if session is None or session.closed or seq != self.seq:
            return
        try:
            items = session.client.id_index(kind, category)
            local = self._local_ids(session, kind)
        except data_import.ExportServerError as e:
            _log(f'[import] Index failed: {e}')
            items, local = [], set()
        self._got_new_items(session, [item for item in items if item.get('id') and item['id'] not in local])

    @run_in_gui_thread
    def _got_new_items(self, session, items):
        if session is self.session:
            self.new_items = items
            self.refresh_controls()

    @run_in_thread('data-import')
    def _load_preview(self, session, kind, category, period, contact, seq):
        if session is None or session.closed or seq != self.seq:
            return
        try:
            ids = session.client.ids(kind, category, period, contact)
            local = self._local_ids(session, kind)
        except data_import.ExportServerError as e:
            self._preview_failed(session, seq, str(e))
            return
        self._got_preview(session, seq, {'server': len(ids), 'new': sum(1 for message_id in ids if message_id not in local)})

    @run_in_gui_thread
    def _got_preview(self, session, seq, preview):
        if session is self.session and seq == self.seq:
            self.preview = preview
            self.refresh_controls()

    @run_in_gui_thread
    def _preview_failed(self, session, seq, reason):
        if session is self.session and seq == self.seq:
            self.error = reason
            self.refresh_controls()

    @run_in_gui_thread
    def _progress(self, session, done, total):
        if session is self.session:
            self.progress = (done, total)
            self.notice = None
            self.refresh_controls()

    # -- the import (worker thread) ---------------------------------------------------------------

    @staticmethod
    def _entry(row, account_id):
        """A journal-shaped entry for a row of the other device; imported history is read history."""
        message, direction, status, encryption, cpim_from, cpim_to = data_import.journal_entry(row, account_id)
        message['state'] = status
        return message

    @staticmethod
    def _apply(session, entries):
        if entries:
            _wait(lambda callback: MessageManager().apply_imported_messages(session.account, list(entries), finished=callback))

    @run_in_thread('data-import')
    def _import(self, session, kind, category, period, contact):
        source = ((self.summary or {}).get('device') or {}).get('useragent') or session.client.server
        _log(f'[import] Importing {kind}/{category}/{period}/{contact or "all"} from {source} into {session.account_id}')
        result = {'server': 0, 'new': 0, 'imported': 0, 'cancelled': False, 'files_failed': 0}
        try:
            if kind == 'files':
                self._import_files(session, category, period, contact, result)
            else:
                self._import_messages(session, category, period, contact, result)
        except data_import.ExportServerError as e:
            _log(f'[import] Import failed: {e}')
            result['error'] = str(e)
        except Exception as e:
            ActivityLog().error(f'[import] Import failed: {e!r}')
            result['error'] = str(e)
        _log(f'[import] Imported {result["imported"]} of {result["new"]} new ({result["server"]} offered)' + (', cancelled' if result['cancelled'] else '')
             + (f', {result["files_failed"]} files failed' if result['files_failed'] else ''))
        session.inventory = None
        if result['imported']:
            MessageManager().imported_messages_done(session.account)
        self._finished(session, result)

    def _import_messages(self, session, category, period, contact, result):
        rows = session.client.rows_bulk('messages', category, period, contact)
        local = self._local_ids(session, 'messages')
        new = [row for row in rows if row.get('msg_id') and row['msg_id'] not in local]
        result['server'], result['new'] = len(rows), len(new)
        self._progress(session, 0, len(new))
        for start in range(0, len(new), MESSAGE_BATCH):
            if self.cancelled or session.closed:
                result['cancelled'] = True
                break
            entries = []
            for row in new[start:start + MESSAGE_BATCH]:
                try:
                    entries.append(self._entry(row, session.account_id))
                except Exception as e:
                    ActivityLog().warning(f'[import] Cannot map message {row.get("msg_id")}: {e!r}')
            self._apply(session, entries)
            result['imported'] += len(entries)
            self._progress(session, min(start + MESSAGE_BATCH, len(new)), len(new))

    def _import_files(self, session, category, period, contact, result):
        from blink.file_transfer import transfer_filename, transfer_folder
        from blink.uris import canonical_uri
        ids = session.client.ids('files', category, period, contact)
        message_ids, missing = self._inventory(session)
        local = message_ids - set(missing)
        new = [message_id for message_id in ids if message_id not in local]
        result['server'], result['new'] = len(ids), len(new)
        self._progress(session, 0, len(new))
        root = ApplicationData.get('file_transfers')
        pending = []
        for done, message_id in enumerate(new, 1):
            if self.cancelled or session.closed:
                result['cancelled'] = True
                break
            try:
                row = session.client.row(message_id)
            except data_import.ExportServerError as e:
                _log(f'[import] No row for {message_id}: {e}')
                result['files_failed'] += 1
                continue
            if message_id in missing:
                peer, meta = missing[message_id]       # the message is here, only its file is not: no row is written
            else:
                peer, meta = data_import.other_party(row, session.account_id), data_import.transfer_envelope(row)
            try:
                payload = session.client.blob(message_id)
            except data_import.ExportServerError as e:
                _log(f'[import] No file for {message_id} ({meta.get("filename")}): {e}')
                payload = None
            if payload is not None and data_import.looks_like_pgp(payload, meta):
                try:
                    payload = _decrypt_file(session.account, payload)
                except Exception as e:
                    ActivityLog().error(f'[import] Cannot decrypt {meta.get("filename")}: {e}')
                    payload = None
            if payload is not None:
                name = transfer_filename(meta.get('filename'))
                name = name[:-4] if name.endswith('.asc') else name
                folder = transfer_folder(root, session.account_id, canonical_uri(str(peer), session.account) or str(peer),
                                         meta.get('transfer_id') or message_id)
                path = os.path.join(folder, name)
                try:
                    _write_file(path, payload)
                    _log(f'[import] Saved {name} ({len(payload)} bytes) in {folder}')
                except OSError as e:
                    ActivityLog().error(f'[import] Cannot save {name}: {e}')
                    payload = None
            if payload is None:
                result['files_failed'] += 1
            if message_id not in missing:
                # the row goes in even without its bytes: the file can still be fetched from the server while it keeps it
                pending.append(self._entry(row, session.account_id))
            elif payload is None:
                continue
            result['imported'] += 1
            if len(pending) >= FILE_BATCH:
                self._apply(session, pending)
                pending = []
            self._progress(session, done, len(new))
        self._apply(session, pending)

    @run_in_gui_thread
    def _finished(self, session, result):
        if session is not self.session:
            return
        self.busy = False
        self.cancelled = False
        self.progress = None
        self.result = result
        self.error = result.get('error')
        self.refresh_controls()
        # what is still missing, counted again
        self._load_new_items(session, self.kind, self.category, self.seq)
        self._load_preview(session, self.kind, self.category, self.period(), self.contact, self.seq)


def partial_retry(panel):
    def on_retry(path, attempt, attempts, reason):
        # in the data-import thread, between two attempts of one request
        _log(f'[import] {path} failed ({reason}), retry {attempt} of {attempts - 1}')
        panel._notice(panel.session, _tr('Connection interrupted, retrying (%d of %d)…') % (attempt, attempts - 1))
    return on_retry


# Export to device

def _export_device():
    from blink import __info__
    return {'name': socket.gethostname(), 'platform': 'linux', 'app_version': __info__.__version__,
            'useragent': str(SIPSimpleSettings().user_agent or 'Blink')}


class ExportPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.accounts = []
        self.account_id = None
        self.server = None
        self.starting = False
        self.error = None
        self.announced_at = None
        self.started_at = None
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_controls)
        self._build()
        self.refresh_accounts()

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)
        self.account_box = QComboBox(self)
        self.account_box.activated.connect(self._SH_AccountChanged)
        form.addRow(_tr('Account'), self.account_box)

        intro = QLabel(_tr("Copy this computer's messages and files to another device of the same account: Sylk Mobile, or Blink on another "
                           "computer. When the export starts, your other devices are told where to find it and open their Import screen, where "
                           "you choose what to copy. Both devices must be on the same network; everything sent is encrypted with a key only your "
                           "devices know. Contacts are not exported: they sync through the server."), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.headline = QLabel(self)
        self.headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.headline.setWordWrap(True)
        font = QFont(self.headline.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.headline.setFont(font)
        layout.addWidget(self.headline)
        self.details = QLabel(self)
        self.details.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.status = QLabel(self)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.announce_button = QPushButton(_tr('Announce Again'), self)
        self.announce_button.setToolTip(_tr('Tell your other devices again: a device that was off or asleep missed the first announcement'))
        self.announce_button.clicked.connect(self._SH_AnnounceClicked)
        buttons.addWidget(self.announce_button)
        self.start_button = QPushButton(_tr('Start Export'), self)
        self.start_button.clicked.connect(self._SH_StartStopClicked)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)

    def refresh_accounts(self):
        if self.server is not None:
            return
        self.accounts = [account for account in AccountManager().iter_accounts() if account.enabled and account is not BonjourAccount()]
        ids = [str(account.id) for account in self.accounts]
        if self.account_id not in ids:
            default = AccountManager().default_account
            self.account_id = str(default.id) if default is not None and str(default.id) in ids else (ids[0] if ids else None)
        _fill(self.account_box, [(str(account.id), mangled_account_label(str(account.id))) for account in self.accounts], self.account_id)
        self.refresh_controls()

    def _account(self):
        return next((account for account in self.accounts if str(account.id) == self.account_id), None)

    @staticmethod
    def _why_not(account):
        if account is None:
            return _tr('There is no account to export')
        if MessageManager()._own_public_key(account) is None:
            return _tr('This account has no PGP key on this computer (or PGP is off): the announcement to the other devices is encrypted with it')
        return None

    def refresh_controls(self):
        server = self.server
        account = self._account()
        reason = self._why_not(account)
        self.account_box.setEnabled(server is None and not self.starting and len(self.accounts) > 1)
        if server is not None:
            snapshot = server.snapshot
            self.headline.setText(_tr('Exporting %d messages and %d files (%s)') % (len(snapshot.messages), len(snapshot.files), _size(snapshot.file_bytes)))
            self.details.setText(f'{server.url}\n{_tr("Key")} {server.token}')
            if server.clients:
                ago = int(time.monotonic() - (server.last_request_at or time.monotonic()))
                status = _tr('%s is importing: %d requests, the last %d s ago (%s)') % (', '.join(sorted(server.clients)), server.requests, ago, server.last_event or '')
            else:
                waited = int(time.monotonic() - (self.announced_at or self.started_at or time.monotonic()))
                status = _tr('Waiting for your other device. Announced %d s ago; if its Import screen did not open, wake it up, '
                             'open Sylk and press Announce Again.') % waited
        elif self.starting:
            self.headline.setText(_tr('Reading the history…'))
            self.details.setText('')
            status = ''
        else:
            self.headline.setText(_tr('Not exporting'))
            self.details.setText('')
            status = reason or ''
        self.status.setText(self.error or status)
        self.status.setStyleSheet('color: #e53935;' if self.error else '')
        self.start_button.setText(_tr('Stop Export') if server is not None else _tr('Start Export'))
        self.start_button.setEnabled(server is not None or (not self.starting and reason is None))
        self.announce_button.setVisible(server is not None)

    def _SH_AccountChanged(self, index):
        self.account_id = self.account_box.itemData(index)
        self.error = None
        self.refresh_controls()

    def _SH_StartStopClicked(self):
        if self.server is not None:
            self.stop()
            return
        account = self._account()
        if self.starting or self._why_not(account) is not None:
            return
        if data_export.local_ipv4() is None:
            self.error = _tr('This computer has no network address: connect it to the same Wi-Fi or Ethernet as your other device.')
            self.refresh_controls()
            return
        self.starting = True
        self.error = None
        self.refresh_controls()
        self._start(account, _export_device())

    @run_in_thread('data-export')
    def _start(self, account, device):
        server = error = None
        try:
            started = time.monotonic()
            rows = _wait(lambda callback: MessageHistory().data_export_rows(str(account.id), callback)) or []
            storage = data_export.TransferStorage(ApplicationData.get('file_transfers'), ApplicationData.get('downloads'))
            snapshot = data_export.ExportSnapshot(str(account.id), rows, file_transfer_envelope, storage)
            server = data_export.ExportServer(snapshot, device)
            server.on_error = lambda text: ActivityLog().warning(f'[export] {text}')
            server.start()
            present = sum(1 for item in snapshot.files if item[3])
            _log(f'[export] Exporting {account.id} at {server.url}: {len(snapshot.messages)} messages, {len(snapshot.files)} files'
                 f' ({present} of them on this computer, {snapshot.file_bytes} bytes), read in {time.monotonic() - started:.1f}s')
        except Exception as e:
            ActivityLog().error(f'[export] Cannot start the export of {account.id}: {e!r}')
            error = _tr('The export could not start: %s') % e
            server = None
        self._started(account, server, error)

    @run_in_gui_thread
    def _started(self, account, server, error):
        self.starting = False
        self.error = error
        if server is not None:
            self.server = server
            self.started_at = time.monotonic()
            self._announce(account)
            self._timer.start()
        self.refresh_controls()

    def _announce(self, account):
        if self.server is not None and MessageManager().send_data_export_announcement(account, self.server.announcement()):
            self.announced_at = time.monotonic()

    def _SH_AnnounceClicked(self):
        account = self._account()
        if account is not None and self.server is not None:
            self._announce(account)
            self.refresh_controls()

    def stop(self):
        """Stop the export, if one runs: Stop, or the Data window closing."""
        self._timer.stop()
        server, self.server = self.server, None
        if server is not None:
            server.stop()
            _log(f'[export] Export of {self.account_id} stopped after {server.requests} requests')
        self.announced_at = self.started_at = None
        self.refresh_controls()
