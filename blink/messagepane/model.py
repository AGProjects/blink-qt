"""ConversationModel: the rows of one conversation, oldest first, paged from history.

Rows are MessageItems, snapshots of history rows taken in the db thread, one
per message id (the same message filed under two accounts is one row). A row is
renderable when history classified it (category set); the rest (keys, tokens,
receipts) never become rows. Opening loads the newest page of 50 renderable
rows; scrolling up loads the page before it (load_older), inserted at the top
so the view can keep what the user was looking at in place. New and changed
messages are merged in by id, in timestamp order, from the newest page.

jump_to(day) loads the page ending with that day instead; the newer messages
then come a page at a time as the view reaches the bottom (load_newer), and
live messages wait until they are reached.

search(text) shows the conversation's text messages containing the text
instead (newest 200, oldest first); search('') goes back to the conversation.

Files being sent over HTTP (blink.messagepane.uploads) are rows too, after the
newest page, until a message of their transfer is in history.
"""

import bisect

from datetime import datetime, time, timedelta, timezone

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from PyQt6.QtCore import Qt, QAbstractListModel, QModelIndex, QTimer, pyqtSignal

from sipsimple.threading import run_in_thread

from blink.logging import ActivityLog, MessagingTrace as log
from blink.util import call_in_gui_thread, run_in_gui_thread, translate


__all__ = ['ConversationModel', 'MessageItem']


class MessageItem(object):
    """What the pane needs of one history row, detached from the database."""

    __slots__ = ('id', 'account_id', 'remote_uri', 'display_name', 'uri', 'timestamp', 'direction', 'content', 'content_type',
                 'state', 'encryption_type', 'decrypted', 'disposition', 'read', 'category', 'has_link', 'metadata',
                 'related_msg_id', 'related_action', 'media_type', 'row_id', 'reply', 'caption', 'peaks', 'upload')

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
        self.reply = None           # what it answers: {'id', 'timestamp', 'outgoing', 'name', 'text'} (attach_replies)
        self.caption = ''           # a picture's or movie's caption (label companion, the newest wins)
        self.peaks = None           # a recording's waveform samples (peaks companion), when the sender made one
        self.upload = None          # a file being sent over HTTP: not in history yet (for_upload)

    @classmethod
    def for_upload(cls, upload):
        """The row of a file being sent (blink.messagepane.uploads), drawn as its message will be."""
        import json
        from blink.message_envelopes import file_transfer_category
        item = cls.__new__(cls)
        content = json.dumps({'filename': upload.name, 'filesize': upload.size, 'filetype': upload.type, 'transfer_id': upload.id})
        item.id = upload.id
        item.row_id = float('inf')      # after the rows of history at the same time
        item.account_id = upload.account_id
        item.remote_uri = upload.key
        item.display_name = ''
        item.uri = ''
        item.timestamp = upload.timestamp
        item.direction = 'outgoing'
        item.content = content
        item.content_type = 'application/sylk-file-transfer'
        item.state = {'uploading': 'pending', 'uploaded': 'sent'}.get(upload.state, 'failed-local')
        item.encryption_type = ''
        item.decrypted = None
        item.disposition = ''
        item.read = True
        item.category = file_transfer_category(content) or 'other'
        item.has_link = False
        item.metadata = None
        item.related_msg_id = item.related_action = item.media_type = None
        item.reply = None
        item.caption = ''
        item.peaks = None
        item.upload = {'path': upload.path, 'state': upload.state, 'reason': upload.reason}
        return item

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


def attach_replies(items):
    """Fill in item.reply for the replies among items, from their reply links (metadata
    companions filed against them) and the messages they answer. In the db thread."""
    from blink.history import Message, MessageHistory
    from blink.message_envelopes import label_metadata, peaks_metadata, reply_metadata
    from blink.messagepane.format import plain_summary
    if not items:
        return
    by_id = {item.id: item for item in items}
    try:
        companions = MessageHistory().related_messages(list(by_id))
    except Exception as e:
        log.warning(f'Cannot read the reply links of {len(items)} messages: {e!r}')
        return
    caption_times = {}
    for companion in companions:
        if companion.related_action == 'label':
            label = label_metadata(companion.content)
            if label is not None and label['transfer_id'] in by_id:
                stamp = (label['timestamp'], str(companion.timestamp))
                if stamp >= caption_times.get(label['transfer_id'], ('', '')):
                    caption_times[label['transfer_id']] = stamp
                    by_id[label['transfer_id']].caption = label['label']
            continue
        if companion.related_action == 'peaks':
            peaks = peaks_metadata(companion.content)
            if peaks is not None and peaks['transfer_id'] in by_id:
                by_id[peaks['transfer_id']].peaks = tuple(peaks['peaks']['l'] or peaks['peaks']['r'])
            continue
        if companion.related_action != 'reply':
            continue
        link = reply_metadata(companion.content)
        if link is None or link['reply_id'] not in by_id:
            continue
        original = by_id.get(link['original_id'])
        if original is None:
            rows = list(Message.selectBy(message_id=link['original_id']))
            original = MessageItem(rows[0]) if rows else None
        if original is None:
            reply = {'id': link['original_id'], 'timestamp': None, 'outgoing': None, 'name': '', 'text': translate('message_pane', 'A message that is not here')}
        else:
            reply = {'id': original.id, 'timestamp': original.timestamp, 'outgoing': original.outgoing,
                     'name': '' if original.outgoing else original.display_name, 'text': plain_summary(original)}
        by_id[link['reply_id']].reply = reply


def _uploads_for(key):
    from blink.messagepane.uploads import Uploads
    return Uploads.instance().for_key(key)


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
    jumped = pyqtSignal(int)            # the row a jump to a day landed on

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.items = []
        self.ids = {}           # message id: item
        self.has_more = False
        self.has_newer = False      # after a jump: newer messages not loaded yet
        self.loading = False
        self.loaded = False
        self.closed = False
        self.search_text = ''
        self._generation = 0    # a reload makes answers to earlier queries stale
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(int(self.refresh_delay * 1000))
        self._refresh_timer.timeout.connect(self._refresh)
        from blink.messagepane.uploads import Uploads
        Uploads.instance().changed.connect(self._SH_UploadChanged)
        notification_center = NotificationCenter()
        for name in ('BlinkMessageHistoryMessageDidStore', 'BlinkMessageHistoryConversationDidRemove', 'BlinkGotHistoryMessageDelete',
                     'BlinkMessageWillDelete', 'BlinkMessageDidDecrypt', 'BlinkJournalDidApply', 'BlinkMessageHistoryCallHistoryDidStore',
                     'BlinkMessageDidSucceed', 'BlinkMessageDidFail', 'BlinkGotDispositionNotification', 'BlinkDidSendDispositionNotification',
                     'BlinkMessageHistoryConversationWasRead', 'BlinkMessageHistoryCompanionDidStore'):
            notification_center.add_observer(self, name=name)

    def close(self):
        """Stop following history (the pane dropped this conversation)."""
        if self.closed:
            return
        self.closed = True
        self._refresh_timer.stop()
        from blink.messagepane.uploads import Uploads
        Uploads.instance().changed.disconnect(self._SH_UploadChanged)
        notification_center = NotificationCenter()
        for name in ('BlinkMessageHistoryMessageDidStore', 'BlinkMessageHistoryConversationDidRemove', 'BlinkGotHistoryMessageDelete',
                     'BlinkMessageWillDelete', 'BlinkMessageDidDecrypt', 'BlinkJournalDidApply', 'BlinkMessageHistoryCallHistoryDidStore',
                     'BlinkMessageDidSucceed', 'BlinkMessageDidFail', 'BlinkGotDispositionNotification', 'BlinkDidSendDispositionNotification',
                     'BlinkMessageHistoryConversationWasRead', 'BlinkMessageHistoryCompanionDidStore'):
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

    search_limit = 200

    def search(self, text):
        """Show the messages containing text; an empty text shows the conversation again."""
        text = (text or '').strip()
        if text == self.search_text:
            return
        self.search_text = text
        if not text:
            self.load()
            return
        self._generation += 1
        self._set_loading(True)
        self._search(self._generation, text)

    @run_in_thread('db')
    def _search(self, generation, text):
        from blink.history import MessageHistory
        try:
            rows = MessageHistory().search_messages(self.key, text, limit=self.search_limit)
        except Exception as e:
            log.warning(f'Searching the conversation with {self.key} failed: {e!r}')
            call_in_gui_thread(self._apply_failed, generation)
            return
        seen, found = set(), []
        for row in rows:
            if row.message_id not in seen and is_renderable(row):
                seen.add(row.message_id)
                found.append(MessageItem(row))
        found.reverse()
        attach_replies(found)
        call_in_gui_thread(self._apply_search, generation, text, found, len(rows) >= self.search_limit)

    def _apply_search(self, generation, text, found, truncated):
        if generation != self._generation or self.closed:
            return
        self.beginResetModel()
        self.items = found
        self.ids = {item.id: item for item in found}
        self.endResetModel()
        self.has_more = False
        self.search_truncated = truncated
        self._set_loading(False)
        ActivityLog().info(f'[Message with {self.key}] Search for {text!r}: {len(found)} messages' + (f' (the newest {self.search_limit})' if truncated else ''))
        self.initialLoadFinished.emit()

    search_truncated = False

    def load(self):
        """The newest page: what opening the conversation shows."""
        self._generation += 1
        self._set_loading(True)
        self._fetch(self._generation, 'initial', before=None)

    def jump_to(self, day):
        """Load the page ending with a day (a local date): the day's messages and the ones before them."""
        if self.search_text:
            self.search_text = ''
        self._generation += 1
        self._set_loading(True)
        end = datetime.combine(day + timedelta(days=1), time()).astimezone()      # local midnight after the day
        self._fetch(self._generation, ('jump', day), before=end)

    def load_newer(self):
        """After a jump: the page after the newest loaded row."""
        if self.loading or not self.has_newer or not self.items or self.closed:
            return
        self._set_loading(True)
        self._fetch_forward(self._generation, self.items[-1].timestamp - self._tick)

    @run_in_thread('db')
    def _fetch_forward(self, generation, after):
        from blink.history import MessageHistory
        history = MessageHistory()
        after = after.astimezone(timezone.utc).replace(tzinfo=None)
        found, seen, more = [], set(), True
        cursor = after
        try:
            while len(found) < self.page_size:
                rows = history.get_messages(self.key, after=cursor, limit=self.fetch_size, oldest_first=True)
                for row in rows:      # oldest first
                    if row.message_id not in seen and is_renderable(row):
                        seen.add(row.message_id)
                        found.append(MessageItem(row))
                if len(rows) < self.fetch_size:
                    more = False
                    break
                last = rows[-1].timestamp
                cursor = last if last - self._tick == cursor else last - self._tick
        except Exception as e:
            log.warning(f'Loading newer messages of the conversation with {self.key} failed: {e!r}')
            call_in_gui_thread(self._apply_failed, generation)
            return
        if len(found) > self.page_size:
            found = found[:self.page_size]
            more = True
        attach_replies(found)
        call_in_gui_thread(self._apply_newer, generation, found, more)

    def _apply_newer(self, generation, found, more):
        if generation != self._generation or self.closed:
            return
        newest = self.items[-1].sort_key if self.items else None
        newer = [item for item in found if item.id not in self.ids and (newest is None or item.sort_key > newest)]
        if newer:
            self.beginInsertRows(QModelIndex(), len(self.items), len(self.items) + len(newer) - 1)
            self.items.extend(newer)
            self.ids.update((item.id, item) for item in newer)
            self.endInsertRows()
        self.has_newer = more and bool(newer)
        self._set_loading(False)
        ActivityLog().info(f'[Message with {self.key}] Loaded {len(newer)} newer messages, {len(self.items)} shown' + (', newer ones available' if self.has_newer else ', up to the newest'))
        if not self.has_newer:
            self._schedule_refresh()        # what arrived in the meantime (and the files being sent)

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
        newer = False
        if isinstance(kind, tuple):     # a jump: is there anything after the page?
            try:
                newer = bool(history.get_messages(self.key, after=before - self._tick, limit=1, oldest_first=True)) if before is not None else False
            except Exception:
                newer = True
        attach_replies(found)
        call_in_gui_thread(self._apply_page, generation, kind, found, more, newer)

    def _apply_failed(self, generation):
        if generation == self._generation:
            self._set_loading(False)

    def _apply_page(self, generation, kind, found, more, newer=False):
        if generation != self._generation or self.closed:
            return
        if isinstance(kind, tuple):
            day = kind[1]
            self.search_truncated = False
            self.beginResetModel()
            self.items = found
            self.ids = {item.id: item for item in found}
            self.endResetModel()
            self.has_more = more
            self.has_newer = newer
            self.loaded = True
            self._set_loading(False)
            row = next((position for position, item in enumerate(found) if item.timestamp.astimezone().date() >= day), max(len(found) - 1, 0))
            ActivityLog().info(f'[Message with {self.key}] Jumped to {day:%Y-%m-%d}: {len(found)} messages loaded' + (', newer ones available' if newer else ''))
            self.jumped.emit(row)
            return
        if kind == 'initial':
            self.has_newer = False
            self.search_truncated = False
            self.beginResetModel()
            found = self._with_uploads(found)
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

    def row_of(self, message_id):
        item = self.ids.get(message_id)
        return self.items.index(item) if item is not None else None

    def remove_item(self, message_id):
        """Take a message out at once (deleted here), wherever it is in what is loaded."""
        item = self.ids.pop(message_id, None)
        if item is None:
            return
        position = self.items.index(item)
        self.beginRemoveRows(QModelIndex(), position, position)
        del self.items[position]
        self.endRemoveRows()

    # Live changes: merged from the newest page

    def _schedule_refresh(self):
        if not self.closed and self.loaded and not self.search_text:
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
        attach_replies(found)
        call_in_gui_thread(self._merge, generation, found, oldest)

    def _merge(self, generation, found, oldest):
        """Merge the newest rows: new ones inserted in order, changed ones updated, and loaded
        rows inside the fetched range that are no longer there (removed) taken out."""
        if generation != self._generation or self.closed:
            return
        fresh = {item.id: item for item in found}
        settled = self._settle_uploads(found)
        for position in reversed(range(len(self.items))):
            item = self.items[position]
            if item.upload is not None and item.id not in settled:
                continue                # a file being sent: not in history yet
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
                if self.has_newer and self.items and item.sort_key > self.items[-1].sort_key:
                    continue            # after a jump: comes when the view gets there
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
        if not self.has_newer:
            for upload in _uploads_for(self.key):
                if upload.id not in self.ids:
                    self._insert(MessageItem.for_upload(upload))

    # Files being sent (blink.messagepane.uploads)

    def _settle_uploads(self, found):
        """The ids of the uploads whose message is among found (forgotten by Uploads)."""
        from blink.messagepane.files import transfer_ids
        from blink.messagepane.uploads import Uploads
        uploads = Uploads.instance()
        if not uploads.for_key(self.key):
            return set()
        ids = set()
        for item in found:
            if item.category in ('image', 'audio', 'video', 'other'):
                ids.update(transfer_ids(item))
        settled = uploads.settle(self.key, ids)
        if settled:
            ActivityLog().info(f'[Message with {self.key}] Sent files now in history: {", ".join(sorted(settled))}')
        return settled

    def _with_uploads(self, found):
        """found (a newest page) and the files being sent whose message is not there."""
        self._settle_uploads(found)
        known = {item.id for item in found}
        uploads = [MessageItem.for_upload(upload) for upload in _uploads_for(self.key) if upload.id not in known]
        return sorted(found + uploads, key=lambda item: item.sort_key) if uploads else found

    def _insert(self, item):
        position = bisect.bisect_right([existing.sort_key for existing in self.items], item.sort_key)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.insert(position, item)
        self.ids[item.id] = item
        self.endInsertRows()

    def _SH_UploadChanged(self, key, transfer_id):
        if key != self.key or self.closed or not self.loaded or self.search_text or self.has_newer:
            return
        from blink.messagepane.uploads import Uploads
        upload = Uploads.instance().get(transfer_id)
        current = self.ids.get(transfer_id)
        if upload is None:
            if current is not None and current.upload is not None:
                self.remove_item(transfer_id)
            return
        item = MessageItem.for_upload(upload)
        if current is None:
            self._insert(item)
        elif current.upload is not None:
            position = self.items.index(current)
            self.items[position] = self.ids[transfer_id] = item
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

    def _NH_BlinkMessageHistoryCompanionDidStore(self, notification):
        if notification.data.related_msg_id in self.ids:
            self._schedule_refresh()         # a reply link (or caption) for a message shown

    def _NH_BlinkMessageHistoryConversationWasRead(self, notification):
        if notification.data.count:
            self._schedule_refresh()         # the read flags changed

    def _NH_BlinkMessageDidDecrypt(self, notification):
        self._schedule_refresh()

    def _NH_BlinkJournalDidApply(self, notification):
        self._schedule_refresh()
