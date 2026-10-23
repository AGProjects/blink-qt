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

search(text) shows the days on which the conversation has text messages
containing the text instead, each day whole (the newest 200 hits, on at most the
newest 30 such days, oldest first), so the hits are read in their context;
search('') goes back to the conversation.

Files being sent over HTTP (blink.messagepane.uploads) are rows too, after the
newest page, until a message of their transfer is in history.

set_category(category) shows one category only (image, video, audio, other,
location, call, text, or links: texts with a link), paged from history the same
way; set_category(None) shows everything again.
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
        item.upload = {'path': upload.path, 'state': upload.state, 'reason': upload.reason,
                       'stage': getattr(upload, 'stage', None), 'progress': getattr(upload, 'progress', None)}
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


def _uploads_for(keys):
    from blink.messagepane.uploads import Uploads
    uploads = Uploads.instance()
    return [upload for key in keys for upload in uploads.for_key(key)]


_LOCATION_CONTENT_TYPE = 'application/sylk-location-sharing'


def is_renderable(row):
    """Rows the transcript draws: what history classified (keys, tokens and the like stay out),
    and the coordinate-free location signals (a request, a stop, a meet-up answer or end),
    drawn as centred notes (blink.messagepane.format.location_note)."""
    if row.category is not None:
        return True
    if str(row.content_type or '').lower() != _LOCATION_CONTENT_TYPE:
        return False
    from blink.location import SIGNAL_ACTIONS
    return row.related_action in SIGNAL_ACTIONS


@implementer(IObserver)
class ConversationModel(QAbstractListModel):
    """One conversation (a conversation key, all accounts) for the transcript view.

    For a contact with several addresses, keys are the conversation keys of all of
    them: their messages are shown as one conversation (key is the one the model
    was opened on, for the logs).
    """

    MessageItemRole = Qt.ItemDataRole.UserRole

    page_size = 50              # renderable rows per page
    fetch_size = 100            # history rows per query while filling a page
    refresh_delay = 0.3         # seconds to coalesce live changes

    _tick = timedelta(microseconds=1)

    loadingChanged = pyqtSignal(bool)
    initialLoadFinished = pyqtSignal()
    jumped = pyqtSignal(int)            # the row a jump to a day landed on

    def __init__(self, key, parent=None, keys=None):
        super().__init__(parent)
        self.key = key
        self.keys = tuple(dict.fromkeys([key] + [other for other in (keys or ()) if other]))
        self.items = []
        self.ids = {}           # message id: item
        self.has_more = False
        self.has_newer = False      # after a jump: newer messages not loaded yet
        self.loading = False
        self.loaded = False
        self.closed = False
        self.search_text = ''
        self.category = None        # one category only (set_category), or all
        self._generation = 0    # a reload makes answers to earlier queries stale
        self._decrypting = set()    # message ids handed to the db thread to decrypt (decrypt_rows)
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
                     'BlinkMessageHistoryConversationWasRead', 'BlinkMessageHistoryCompanionDidStore', 'BlinkMessageHistoryCallRecordDidStore',
                     'BlinkMessagesDidDecryptInBackground'):
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
                     'BlinkMessageHistoryConversationWasRead', 'BlinkMessageHistoryCompanionDidStore', 'BlinkMessageHistoryCallRecordDidStore',
                     'BlinkMessagesDidDecryptInBackground'):
            notification_center.discard_observer(self, name=name)

    @property
    def history_key(self):
        """What history is asked about: the key, or all of them."""
        return self.key if len(self.keys) == 1 else list(self.keys)

    @property
    def name(self):
        return self.key if len(self.keys) == 1 else f'{self.key} (+{len(self.keys) - 1} addresses)'

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

    # Decrypting what is in view

    def decrypt_rows(self, first, last):
        """Decrypt the rows first..last (those in view) still holding ciphertext: messages
        stored encrypted (from the journal, or before the key was here) are only opened
        when they are looked at. Each id is tried once per model; a failure is stored
        (decrypted '2') with the reason, as for a live message."""
        from blink.message_envelopes import is_pgp_armoured
        if self.closed or not self.items:
            return
        first, last = max(0, first), min(len(self.items) - 1, last)
        wanted = [(item.id, item.account_id, item.content) for item in self.items[first:last + 1]
                  if item.upload is None and item.id not in self._decrypting and str(item.decrypted) != '2' and is_pgp_armoured(item.content)]
        if not wanted:
            return
        self._decrypting.update(message_id for message_id, _account, _content in wanted)
        import time
        from blink.pgp_decryptor import BackgroundDecryptor
        BackgroundDecryptor().last_view_decrypt = time.time()     # it steps aside while what is in view is opened
        self._decrypt(wanted)

    @run_in_thread('pgp-decrypt')
    def _decrypt(self, wanted):
        """In the decrypting thread shared with BackgroundDecryptor (never the GUI or the db
        thread, so neither the UI nor its history queries wait for it); stored in the db thread."""
        import pgpy
        import warnings
        from blink.pgp_decryptor import BackgroundDecryptor
        decryptor = BackgroundDecryptor()
        results, keyless = [], set()
        for message_id, account_id, content in wanted:
            key = decryptor.key(account_id)
            if key is None:
                keyless.add(account_id)
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    plaintext = key.decrypt(pgpy.PGPMessage.from_blob(content)).message
                if isinstance(plaintext, (bytes, bytearray)):
                    plaintext = bytes(plaintext).decode('utf-8')
            except Exception as e:
                results.append((message_id, account_id, content, None, str(e) or e.__class__.__name__))
            else:
                results.append((message_id, account_id, content, plaintext, None))
        for account_id in keyless:
            log.debug(f'Not decrypting messages of {account_id} in view: no private key here')
        self._store_decrypted(results, keyless, [message_id for message_id, account_id, _content in wanted if account_id in keyless])

    @run_in_thread('db')
    def _store_decrypted(self, results, keyless, keyless_ids):
        from blink.history import MessageHistory
        from blink.pgp_keyids import describe, encrypted_to
        history = MessageHistory()
        opened, failed = {}, 0
        for message_id, account_id, content, plaintext, error in results:
            try:
                if plaintext is not None:
                    rows = history.store_decrypted_message(message_id, plaintext)
                else:
                    failed += 1
                    rows = history.store_undecryptable_message(message_id, error)
                    ActivityLog().warning(f'[pgp] Message {message_id} with {self.name} would not decrypt with the key of {account_id}: {error}; '
                                          f"encrypted to {', '.join(describe(encrypted_to(content))) or 'no key ids'}")
            except Exception as e:
                ActivityLog().error(f'[pgp] Storing decrypted message {message_id} failed: {e}')
                continue
            row = next((row for row in rows if str(row.account_id) == account_id), rows[0] if rows else None)
            if row is not None:
                opened[message_id] = MessageItem(row)
        if opened:
            log.debug(f'Decrypted {len(opened) - failed} messages with {self.name} in view' + (f', {failed} would not decrypt' if failed else ''))
        if opened or keyless_ids:
            call_in_gui_thread(self._apply_decrypted, opened, keyless)

    def _refresh_items(self, message_ids):
        """Rows changed behind the model (BackgroundDecryptor): read again and updated in place."""
        wanted = [(message_id, self.ids[message_id].account_id) for message_id in message_ids if message_id in self.ids]
        if wanted:
            self._read_items(wanted)

    @run_in_thread('db')
    def _read_items(self, wanted):
        from blink.history import Message
        fresh = {}
        for message_id, account_id in wanted:
            rows = list(Message.selectBy(message_id=message_id))
            row = next((row for row in rows if str(row.account_id) == account_id), rows[0] if rows else None)
            if row is not None:
                fresh[message_id] = MessageItem(row)
        if fresh:
            call_in_gui_thread(self._apply_decrypted, fresh)

    def _forget_decrypting(self, message_ids):
        """No key for them yet: tried again when they are in view after one arrives."""
        self._decrypting.difference_update(message_ids)

    def _apply_decrypted(self, opened, keyless=()):
        if self.closed:
            return
        for message_id, fresh in opened.items():
            current = self.ids.get(message_id)
            if current is None:
                continue
            for name in ('reply', 'caption', 'peaks', 'upload', 'row_id'):
                setattr(fresh, name, getattr(current, name))
            if not self._shows(fresh):
                continue        # e.g. no longer in the category shown: goes with the next reload
            position = self.items.index(current)
            self.items[position] = self.ids[message_id] = fresh
            index = self.index(position)
            self.dataChanged.emit(index, index)
        if keyless:
            self._decrypting.difference_update(item.id for item in self.items if item.account_id in keyless)

    # Loading

    search_limit = 200          # hits
    search_day_limit = 30       # days shown, the newest ones with hits
    search_context_limit = 3000 # rows of those days

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
        history = MessageHistory()
        try:
            rows = history.search_messages(self.history_key, text, limit=self.search_limit)
            hits, day_counts = set(), {}
            for row in rows:
                if is_renderable(row) and row.message_id not in hits:
                    hits.add(row.message_id)
                    day = row.timestamp.replace(tzinfo=timezone.utc).astimezone().date().isoformat()     # stored naive UTC
                    day_counts[day] = day_counts.get(day, 0) + 1
            days = sorted(day_counts, reverse=True)[:self.search_day_limit]
            context = history.get_messages_on_days(self.history_key, days, category=self.category, limit=self.search_context_limit)
        except Exception as e:
            log.warning(f'Searching the conversation with {self.name} failed: {e!r}')
            call_in_gui_thread(self._apply_failed, generation)
            return
        seen, found = set(), []
        for row in context:       # oldest first
            if row.message_id not in seen and is_renderable(row):
                seen.add(row.message_id)
                found.append(MessageItem(row))
        attach_replies(found)
        truncated = len(rows) >= self.search_limit or len(day_counts) > len(days) or len(context) >= self.search_context_limit
        call_in_gui_thread(self._apply_search, generation, text, found, hits, {day: day_counts[day] for day in days}, truncated)

    def _apply_search(self, generation, text, found, hits, day_counts, truncated):
        if generation != self._generation or self.closed:
            return
        self.beginResetModel()
        self.items = found
        self.ids = {item.id: item for item in found}
        self.endResetModel()
        self.has_more = False
        self.has_newer = False
        self.search_hits = {item.id for item in found if item.id in hits}
        self.search_day_counts = day_counts
        self.search_truncated = truncated
        self._set_loading(False)
        ActivityLog().info(f'[Message with {self.name}] Search for {text!r}: {len(self.search_hits)} messages on {len(day_counts)} days, {len(found)} messages shown'
                           + (' (the newest only)' if truncated else ''))
        self.initialLoadFinished.emit()
        newest = next((position for position in range(len(found) - 1, -1, -1) if found[position].id in self.search_hits), None)
        if newest is not None:
            self.jumped.emit(newest)    # the newest hit at the top, its day after it

    search_truncated = False
    search_hits = frozenset()       # ids of the rows shown that contain the searched text
    search_day_counts = {}          # 'YYYY-MM-DD': hits that day, for the days shown

    def search_day_row(self, day):
        """The first row of a day shown by the search, or None."""
        for position, item in enumerate(self.items):
            if item.timestamp.astimezone().date() == day:
                return position
        return None

    def set_category(self, category):
        """Show one category only (None: everything), from the newest page."""
        category = category or None
        if category == self.category:
            return
        self.category = category
        ActivityLog().info(f'[Message with {self.name}] Showing ' + (f'{category} only' if category else 'all messages'))
        if not self.search_text:
            self.load()
        else:       # the days found, of that category only
            self._generation += 1
            self._set_loading(True)
            self._search(self._generation, self.search_text)

    def _shows(self, item):
        return self.category is None or item.category == self.category

    def load(self):
        """The newest page: what opening the conversation shows."""
        self._generation += 1
        self._decrypting.clear()    # a key may have arrived or changed since: try again what comes into view
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
                rows = history.get_messages(self.history_key, after=cursor, limit=self.fetch_size, oldest_first=True, category=self.category)
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
            log.warning(f'Loading newer messages of the conversation with {self.name} failed: {e!r}')
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
        ActivityLog().info(f'[Message with {self.name}] Loaded {len(newer)} newer messages, {len(self.items)} shown' + (', newer ones available' if self.has_newer else ', up to the newest'))
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
                rows = history.get_messages(self.history_key, before=cursor, limit=self.fetch_size, category=self.category)
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
            log.warning(f'Loading the conversation with {self.name} failed: {e!r}')
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
                newer = bool(history.get_messages(self.history_key, after=before - self._tick, limit=1, oldest_first=True, category=self.category)) if before is not None else False
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
            ActivityLog().info(f'[Message with {self.name}] Jumped to {day:%Y-%m-%d}: {len(found)} messages loaded' + (', newer ones available' if newer else ''))
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
            ActivityLog().info(f'[Message with {self.name}] Loaded {len(found)} ' + (f'{self.category} ' if self.category else '') + 'messages' + (f' from {found[0].timestamp.astimezone():%Y-%m-%d %H:%M}' if found else '') + (', older ones available' if more else ', the whole conversation'))
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
        ActivityLog().info(f'[Message with {self.name}] Loaded {len(older)} older messages' + (f' from {older[0].timestamp.astimezone():%Y-%m-%d %H:%M}' if older else '') + f', {len(self.items)} shown' + (', older ones available' if self.has_more else ', the whole conversation'))

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
            rows = MessageHistory().get_messages(self.history_key, limit=self.fetch_size, category=self.category)
        except Exception as e:
            log.warning(f'Refreshing the conversation with {self.name} failed: {e!r}')
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
        settled = self._settle_uploads(found)
        sending = {upload.id for upload in _uploads_for(self.keys)}
        found = [item for item in found if item.id not in sending]     # its local copy: the upload's bubble stands for it
        fresh = {item.id: item for item in found}
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
            for upload in _uploads_for(self.keys):
                if upload.id not in self.ids and self._shows(MessageItem.for_upload(upload)):
                    self._insert(MessageItem.for_upload(upload))

    # Files being sent (blink.messagepane.uploads)

    def _settle_uploads(self, found):
        """The ids of the uploads whose message is among found (forgotten by Uploads)."""
        from blink.messagepane.files import transfer_ids
        from blink.messagepane.uploads import Uploads
        uploads = Uploads.instance()
        if not any(uploads.for_key(key) for key in self.keys):
            return set()
        ids = set()
        for item in found:
            if item.category in ('image', 'audio', 'video', 'other'):
                ids.update(transfer_ids(item))
        settled = set()
        for key in self.keys:
            settled |= set(uploads.settle(key, ids) or ())
        if settled:
            ActivityLog().info(f'[Message with {self.name}] Sent files now in history: {", ".join(sorted(settled))}')
        return settled

    def _with_uploads(self, found):
        """found (a newest page) and the files being sent whose message is not there."""
        self._settle_uploads(found)
        sending = {upload.id for upload in _uploads_for(self.keys)}
        found = [item for item in found if item.id not in sending]     # its local copy: the upload's bubble stands for it
        known = {item.id for item in found}
        uploads = [item for item in (MessageItem.for_upload(upload) for upload in _uploads_for(self.keys) if upload.id not in known) if self._shows(item)]
        return sorted(found + uploads, key=lambda item: item.sort_key) if uploads else found

    def _insert(self, item):
        position = bisect.bisect_right([existing.sort_key for existing in self.items], item.sort_key)
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.insert(position, item)
        self.ids[item.id] = item
        self.endInsertRows()

    def _SH_UploadChanged(self, key, transfer_id):
        if key not in self.keys or self.closed or not self.loaded or self.search_text or self.has_newer:
            return
        from blink.messagepane.uploads import Uploads
        upload = Uploads.instance().get(transfer_id)
        current = self.ids.get(transfer_id)
        if upload is None:
            if current is not None and current.upload is not None:
                self.remove_item(transfer_id)
            return
        item = MessageItem.for_upload(upload)
        if not self._shows(item):
            return
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
        if str(notification.data.remote_uri) in self.keys:
            self._schedule_refresh()

    def _NH_BlinkMessageHistoryCallHistoryDidStore(self, notification):
        if str(notification.data.message.remote_uri) in self.keys:
            self._schedule_refresh()

    def _NH_BlinkMessageHistoryCallRecordDidStore(self, notification):
        if str(notification.data.remote_uri) in self.keys:
            self._schedule_refresh()     # a call merged with another view of it (the server's, another device's)

    def _NH_BlinkMessageHistoryConversationDidRemove(self, notification):
        if str(notification.data.contact) in self.keys:
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

    def _NH_BlinkMessagesDidDecryptInBackground(self, notification):
        self._refresh_items(notification.data.ids)

    def _NH_BlinkJournalDidApply(self, notification):
        self._schedule_refresh()
