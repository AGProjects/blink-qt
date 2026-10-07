"""ConversationModel: the rows of one conversation, oldest first, paged from history.

Rows are MessageItems, snapshots of history rows taken in the db thread, one
per message id (the same message filed under two accounts is one row). A row is
renderable when history classified it (category set); the rest (keys, tokens,
receipts) never become rows. Opening loads the newest page of 50 renderable
rows; scrolling up loads the page before it (load_older), inserted at the top
so the view can keep what the user was looking at in place. New and changed
messages are merged in by id, in timestamp order, from the newest page.
"""

import bisect

from datetime import timedelta, timezone

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from PyQt6.QtCore import Qt, QAbstractListModel, QModelIndex, QTimer, pyqtSignal

from sipsimple.threading import run_in_thread

from blink.logging import ActivityLog, MessagingTrace as log
from blink.util import call_in_gui_thread, run_in_gui_thread


__all__ = ['ConversationModel', 'MessageItem']


class MessageItem(object):
    """What the pane needs of one history row, detached from the database."""

    __slots__ = ('id', 'account_id', 'remote_uri', 'display_name', 'uri', 'timestamp', 'direction', 'content', 'content_type',
                 'state', 'encryption_type', 'decrypted', 'disposition', 'read', 'category', 'has_link', 'metadata',
                 'related_msg_id', 'related_action', 'media_type', 'row_id')

    def __init__(self, row):
        self.id = str(row.message_id)
        self.row_id = row.id
        self.account_id = str(row.account_id or '')
        self.remote_uri = str(row.remote_uri or '')
        self.display_name = row.display_name or ''
        self.uri = row.uri or ''
        timestamp = row.timestamp
        self.timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
        self.direction = row.direction
        self.content = row.content
        self.content_type = str(row.content_type or '')
        self.state = row.state
        self.encryption_type = row.encryption_type or ''
        self.decrypted = row.decrypted
        self.disposition = row.disposition or ''
        self.read = row.read
        self.category = row.category
        self.has_link = row.has_link
        self.metadata = row.metadata
        self.related_msg_id = row.related_msg_id
        self.related_action = row.related_action
        self.media_type = row.media_type

    @property
    def sort_key(self):
        return self.timestamp, self.row_id

    @property
    def outgoing(self):
        return self.direction == 'outgoing'

    def same_as(self, other):
        return all(getattr(self, name) == getattr(other, name) for name in self.__slots__ if name != 'row_id')

    def __repr__(self):
        return f'MessageItem({self.id!r}, {self.direction}, {self.content_type}, {self.timestamp.isoformat()})'


def is_renderable(row):
    """Rows the transcript draws: what history classified (keys, tokens and the like stay out)."""
    return row.category is not None


@implementer(IObserver)
class ConversationModel(QAbstractListModel):
    """One conversation (a conversation key, all accounts) for the transcript view."""

    MessageItemRole = Qt.ItemDataRole.UserRole

    page_size = 50              # renderable rows per page
    fetch_size = 100            # history rows per query while filling a page
    refresh_delay = 0.3         # seconds to coalesce live changes

    _tick = timedelta(microseconds=1)

    loadingChanged = pyqtSignal(bool)
    initialLoadFinished = pyqtSignal()

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.items = []
        self.ids = {}           # message id: item
        self.has_more = False
        self.loading = False
        self.loaded = False
        self.closed = False
        self._generation = 0    # a reload makes answers to earlier queries stale
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(int(self.refresh_delay * 1000))
        self._refresh_timer.timeout.connect(self._refresh)
        notification_center = NotificationCenter()
        for name in ('BlinkMessageHistoryMessageDidStore', 'BlinkMessageHistoryConversationDidRemove', 'BlinkGotHistoryMessageDelete',
                     'BlinkMessageWillDelete', 'BlinkMessageDidDecrypt', 'BlinkJournalDidApply', 'BlinkMessageHistoryCallHistoryDidStore',
                     'BlinkMessageDidSucceed', 'BlinkMessageDidFail', 'BlinkGotDispositionNotification', 'BlinkDidSendDispositionNotification'):
            notification_center.add_observer(self, name=name)

    def close(self):
        """Stop following history (the pane dropped this conversation)."""
        if self.closed:
            return
        self.closed = True
        self._refresh_timer.stop()
        notification_center = NotificationCenter()
        for name in ('BlinkMessageHistoryMessageDidStore', 'BlinkMessageHistoryConversationDidRemove', 'BlinkGotHistoryMessageDelete',
                     'BlinkMessageWillDelete', 'BlinkMessageDidDecrypt', 'BlinkJournalDidApply', 'BlinkMessageHistoryCallHistoryDidStore',
                     'BlinkMessageDidSucceed', 'BlinkMessageDidFail', 'BlinkGotDispositionNotification', 'BlinkDidSendDispositionNotification'):
            notification_center.discard_observer(self, name=name)

    # Qt model

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == self.MessageItemRole:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            from blink.messagepane.format import plain_summary
            arrow = '→' if item.outgoing else '←'
            return f"{item.timestamp.astimezone().strftime('%H:%M')} {arrow} {plain_summary(item)}"
        return None

    # Loading

    def load(self):
        """The newest page: what opening the conversation shows."""
        self._generation += 1
        self._set_loading(True)
        self._fetch(self._generation, 'initial', before=None)

    def load_older(self):
        """The page before the oldest loaded row; no-op while loading or when there is none."""
        if self.loading or not self.has_more or not self.items or self.closed:
            return
        self._set_loading(True)
        # just after the oldest row: rows sharing its time that did not fit the last page come too (known ones are dropped)
        self._fetch(self._generation, 'older', before=self.items[0].timestamp + self._tick)

    def _set_loading(self, loading):
        if self.loading != loading:
            self.loading = loading
            self.loadingChanged.emit(loading)

    @run_in_thread('db')
    def _fetch(self, generation, kind, before):
        from blink.history import MessageHistory
        history = MessageHistory()
        found, seen, more = [], set(), True
        if before is not None and before.tzinfo is not None:
            before = before.astimezone(timezone.utc).replace(tzinfo=None)     # as history stores (and returns) times
        cursor = before
        try:
            while len(found) < self.page_size:
                rows = history.get_messages(self.key, before=cursor, limit=self.fetch_size)
                for row in rows:      # newest first
                    if row.message_id not in seen and is_renderable(row):
                        seen.add(row.message_id)
                        found.append(MessageItem(row))
                if len(rows) < self.fetch_size:
                    more = False
                    break
                last = rows[-1].timestamp
                # past the rows of the last time seen, unless a whole query was that one time
                cursor = last if cursor is not None and last + self._tick == cursor else last + self._tick
        except Exception as e:
            log.warning(f'Loading the conversation with {self.key} failed: {e!r}')
            call_in_gui_thread(self._apply_failed, generation)
            return
        if len(found) > self.page_size:
            # rows of the timestamp the page ends on may continue: keep them for the next page
            found = found[:self.page_size]
            more = True
        found.reverse()
        call_in_gui_thread(self._apply_page, generation, kind, found, more)

    def _apply_failed(self, generation):
        if generation == self._generation:
            self._set_loading(False)

    def _apply_page(self, generation, kind, found, more):
        if generation != self._generation or self.closed:
            return
        if kind == 'initial':
            self.beginResetModel()
            self.items = found
            self.ids = {item.id: item for item in found}
            self.endResetModel()
            self.has_more = more
            self.loaded = True
            self._set_loading(False)
            ActivityLog().info(f'[Message with {self.key}] Loaded {len(found)} messages' + (f' from {found[0].timestamp.astimezone():%Y-%m-%d %H:%M}' if found else '') + (', older ones available' if more else ', the whole conversation'))
            self.initialLoadFinished.emit()
            return
        found = [item for item in found if item.id not in self.ids]
        oldest = self.items[0].sort_key if self.items else None
        older = [item for item in found if oldest is None or item.sort_key < oldest]
        if older:
            self.beginInsertRows(QModelIndex(), 0, len(older) - 1)
            self.items[0:0] = older
            self.ids.update((item.id, item) for item in older)
            self.endInsertRows()
        self.has_more = more and bool(older)
        self._set_loading(False)
        ActivityLog().info(f'[Message with {self.key}] Loaded {len(older)} older messages' + (f' from {older[0].timestamp.astimezone():%Y-%m-%d %H:%M}' if older else '') + f', {len(self.items)} shown' + (', older ones available' if self.has_more else ', the whole conversation'))

    # Live changes: merged from the newest page

    def _schedule_refresh(self):
        if not self.closed and self.loaded:
            self._refresh_timer.start()

    def _refresh(self):
        self._fetch_newest(self._generation)

    @run_in_thread('db')
    def _fetch_newest(self, generation):
        from blink.history import MessageHistory
        try:
            rows = MessageHistory().get_messages(self.key, limit=self.fetch_size)
        except Exception as e:
            log.warning(f'Refreshing the conversation with {self.key} failed: {e!r}')
            return
        seen, found = set(), []
        for row in rows:
            if row.message_id not in seen and is_renderable(row):
                seen.add(row.message_id)
                found.append(MessageItem(row))
        complete = len(rows) < self.fetch_size
        oldest = found[-1].sort_key if found and not complete else None
        call_in_gui_thread(self._merge, generation, found, oldest)

    def _merge(self, generation, found, oldest):
        """Merge the newest rows: new ones inserted in order, changed ones updated, and loaded
        rows inside the fetched range that are no longer there (removed) taken out."""
        if generation != self._generation or self.closed:
            return
        fresh = {item.id: item for item in found}
        for position in reversed(range(len(self.items))):
            item = self.items[position]
            if item.id not in fresh and (oldest is None or item.sort_key >= oldest):
                self.beginRemoveRows(QModelIndex(), position, position)
                del self.items[position]
                del self.ids[item.id]
                self.endRemoveRows()
        for item in sorted(found, key=lambda item: item.sort_key):
            current = self.ids.get(item.id)
            if current is None:
                if self.has_more and self.items and item.sort_key < self.items[0].sort_key:
                    continue            # older than what is loaded: comes with its page
                position = bisect.bisect_right([existing.sort_key for existing in self.items], item.sort_key)
                self.beginInsertRows(QModelIndex(), position, position)
                self.items.insert(position, item)
                self.ids[item.id] = item
                self.endInsertRows()
            elif not current.same_as(item):
                position = self.items.index(current)
                self.items[position] = item
                self.ids[item.id] = item
                index = self.index(position)
                self.dataChanged.emit(index, index)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkMessageHistoryMessageDidStore(self, notification):
        if str(notification.data.remote_uri) == self.key:
            self._schedule_refresh()

    def _NH_BlinkMessageHistoryCallHistoryDidStore(self, notification):
        if str(notification.data.message.remote_uri) == self.key:
            self._schedule_refresh()

    def _NH_BlinkMessageHistoryConversationDidRemove(self, notification):
        if str(notification.data.contact) == self.key:
            self.load()

    def _NH_BlinkGotHistoryMessageDelete(self, notification):
        if notification.data.message_id in self.ids:
            self._schedule_refresh()

    def _NH_BlinkMessageWillDelete(self, notification):
        if notification.data.id in self.ids:
            self._schedule_refresh()

    def _NH_BlinkMessageDidSucceed(self, notification):
        if notification.data.id in self.ids:
            self._schedule_refresh()     # the state is stored by HistoryManager first

    _NH_BlinkMessageDidFail = _NH_BlinkMessageDidSucceed
    _NH_BlinkGotDispositionNotification = _NH_BlinkMessageDidSucceed
    _NH_BlinkDidSendDispositionNotification = _NH_BlinkMessageDidSucceed

    def _NH_BlinkMessageDidDecrypt(self, notification):
        self._schedule_refresh()

    def _NH_BlinkJournalDidApply(self, notification):
        self._schedule_refresh()
