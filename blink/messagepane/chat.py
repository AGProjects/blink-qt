"""The transcript of an MSRP session (the MSRP sessions window), drawn as the message pane draws one.

A ChatTranscriptModel holds the rows the window adds as they come (history
loaded at start, messages of the session, its status lines), oldest first, in
memory only: nothing is paged from history. Rows are ChatItems, MessageItems
built by the window rather than read from history; a status line is a note
(a centred line), and a row carries its sender so that the messages of
different participants of a conference are not grouped as one turn.
"""

import itertools
import json
import os

from datetime import datetime, timezone

from PyQt6.QtCore import Qt, QAbstractListModel, QModelIndex, pyqtSignal

from blink.messagepane.model import MessageItem


__all__ = ['ChatItem', 'ChatTranscriptModel', 'SenderAvatars', 'save_inline_image', 'sender_key']


def _utc(timestamp):
    if timestamp is None:
        return datetime.now(timezone.utc)
    return timestamp.astimezone(timezone.utc)      # a naive time is local time


class ChatItem(MessageItem):
    __slots__ = ('note', 'sender', 'private', 'recipient_name')

    _sequence = itertools.count()

    def __init__(self, id, direction='incoming', content='', content_type='text/plain', category='text', timestamp=None,
                 display_name='', uri='', account_id='', remote_uri='', state=None, note=False, sender=None, private=False, recipient_name=''):
        self.id = str(id)
        self.row_id = next(self._sequence)      # keeps the order of rows with the same time
        self.account_id = str(account_id or '')
        self.remote_uri = str(remote_uri or '')
        self.display_name = display_name or ''
        self.uri = uri or ''
        self.timestamp = _utc(timestamp)
        self.direction = direction
        self.content = content
        self.content_type = content_type or ''
        self.state = state
        self.encryption_type = ''
        self.decrypted = None
        self.disposition = ''
        self.read = True
        self.category = category
        self.has_link = False
        self.metadata = None
        self.related_msg_id = self.related_action = self.media_type = None
        self.reply = None
        self.caption = ''
        self.peaks = None
        self.upload = None
        self.note = note
        self.sender = sender
        self.private = private                  # a private message in a conference (CPIM To: one participant)
        self.recipient_name = recipient_name    # whom an outgoing private message went to


def save_inline_image(message_id, content_type, data):
    """An image sent inside a chat message (not as a file transfer) kept as a file, where
    blink.messagepane.files.local_file finds it, and the file transfer envelope that draws it.
    Returns the envelope, or None when it cannot be kept."""
    from blink.file_transfer import safe_component
    from blink.resources import ApplicationData
    extension = {'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/svg+xml': 'svg'}.get(content_type.lower(), content_type.split('/')[-1].lower() or 'png')
    name = f'image.{safe_component(extension)}'
    folder = os.path.join(ApplicationData.get('downloads'), safe_component(message_id))
    path = os.path.join(folder, name)
    try:
        if not os.path.isfile(path):
            os.makedirs(folder, exist_ok=True)
            with open(path, 'wb') as image:
                image.write(data)
    except OSError:
        return None
    return json.dumps({'filename': name, 'filesize': len(data), 'filetype': content_type, 'transfer_id': str(message_id)})


class ChatTranscriptModel(QAbstractListModel):
    """The rows of one MSRP session's transcript, in time order (for TranscriptView).

    all_items holds every row; items the ones shown: all of them, or while searching
    (search(), the strip's search field) the messages containing the text, status
    lines left out. ids finds any row, shown or not.
    """

    MessageItemRole = Qt.ItemDataRole.UserRole

    # what TranscriptView asks of a model that pages: this one has everything already
    has_more = False
    has_newer = False
    loading = False
    loaded = True
    search_truncated = False
    category = None

    loadingChanged = pyqtSignal(bool)
    initialLoadFinished = pyqtSignal()
    jumped = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.all_items = []
        self.items = []
        self.ids = {}
        self.search_text = ''

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
            return plain_summary(item)
        return None

    # Searching

    def matches(self, item):
        if not self.search_text:
            return True
        if item.note:
            return False
        from blink.messagepane.format import plain_summary
        text = plain_summary(item)
        if item.category in ('image', 'audio', 'video', 'other'):
            from blink.messagepane.files import file_info
            info = file_info(item)
            if info:
                text += ' ' + info['name']
        return self.search_text.casefold() in text.casefold()

    def search(self, text):
        """Show only the messages containing text ('' shows all again)."""
        text = (text or '').strip()
        if text == self.search_text:
            return
        self.beginResetModel()
        self.search_text = text
        self.items = [item for item in self.all_items if self.matches(item)]
        self.endResetModel()

    # Rows

    def row_of(self, message_id):
        """The row a message is shown in, None when it is not here or not shown (searching)."""
        item = self.ids.get(str(message_id))
        if item is None:
            return None
        try:
            return self.items.index(item)
        except ValueError:
            return None

    def get(self, message_id):
        return self.ids.get(str(message_id))

    def add(self, item):
        """Add a row in time order; a row with the id of one already here replaces it."""
        if item.id in self.ids and not item.note:
            self.replace(item.id, item)
            return
        import bisect
        keys = [(row.timestamp, row.row_id) for row in self.all_items]
        self.all_items.insert(bisect.bisect_right(keys, (item.timestamp, item.row_id)), item)
        if not item.note:
            self.ids[item.id] = item
        if not self.matches(item):
            return
        keys = [(row.timestamp, row.row_id) for row in self.items]
        position = bisect.bisect_right(keys, (item.timestamp, item.row_id))
        self.beginInsertRows(QModelIndex(), position, position)
        self.items.insert(position, item)
        self.endInsertRows()
        self._neighbours_changed(position)

    def replace(self, message_id, item):
        old = self.ids.get(str(message_id))
        if old is None:
            self.add(item)
            return
        item.row_id = old.row_id
        item.timestamp = old.timestamp
        self.all_items[self.all_items.index(old)] = item
        self.ids.pop(str(message_id), None)
        self.ids[item.id] = item
        row = self.items.index(old) if old in self.items else None
        if row is not None:
            if self.matches(item):
                self.items[row] = item
                self._changed(row)
            else:
                self.beginRemoveRows(QModelIndex(), row, row)
                self.items.pop(row)
                self.endRemoveRows()
                self._neighbours_changed(row - 1)

    def update(self, message_id, **changes):
        """Change some fields of a row (its state, its content); False when it is not here."""
        item = self.ids.get(str(message_id))
        if item is None:
            return False
        for name, value in changes.items():
            setattr(item, name, value)
        row = self.row_of(message_id)
        if row is not None:
            self._changed(row)
        return True

    def refresh(self, message_id):
        row = self.row_of(message_id)
        if row is not None:
            self._changed(row)

    def remove(self, message_id):
        item = self.ids.pop(str(message_id), None)
        if item is None:
            return None
        self.all_items.remove(item)
        if item in self.items:
            row = self.items.index(item)
            self.beginRemoveRows(QModelIndex(), row, row)
            self.items.pop(row)
            self.endRemoveRows()
            self._neighbours_changed(row - 1)
        return item

    def clear(self):
        self.beginResetModel()
        self.all_items = []
        self.items = []
        self.ids = {}
        self.endResetModel()

    def _changed(self, row):
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)
        self._neighbours_changed(row)

    def _neighbours_changed(self, row):
        # a row's neighbours decide whether it starts a turn or a day: they are laid out again
        first, last = max(row - 1, 0), min(row + 1, len(self.items) - 1)
        if first <= last:
            self.dataChanged.emit(self.index(first, 0), self.index(last, 0))


def sender_key(item):
    """Who wrote a row, as an address without sip:: the sender a row of the MSRP window carries, else
    the CPIM From history keeps in uri. In a conference (the conversation is the room) this, not
    the conversation, tells the participants apart."""
    key = getattr(item, 'sender', None) or getattr(item, 'uri', None) or getattr(item, 'remote_uri', None) or ''
    key = str(key)
    for prefix in ('sips:', 'sip:'):
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


class SenderAvatars(object):
    """The name and avatar of the participant who wrote each message of a conference, from its
    sender (sender_key): the same picture, initials and colour as in the contact list. A contact
    in the addressbook gives the name, else the name the message came with, else the address."""

    def __init__(self):
        self._known = {}        # sender key: (name, photo, letters, colour)

    def clear(self):
        self._known.clear()

    def _lookup(self, item):
        key = sender_key(item)
        known = self._known.get(key)
        if known is None:
            known = self._known[key] = self._describe(key, getattr(item, 'display_name', '') or '')
        return known

    @staticmethod
    def _describe(key, display_name):
        from PyQt6.QtGui import QColor
        from blink.avatars import avatar_color, avatar_initials
        from blink.contact_mangler import mangled_name, mangling_enabled
        from blink.messagepane.header import contact_photo
        contact = None
        if key:
            try:
                from blink.contacts import URIUtils
                contact, contact_uri = URIUtils.find_contact(key)
            except Exception:
                contact = None
        known = contact is not None and getattr(contact, 'type', None) not in (None, 'dummy')
        name = (getattr(contact, 'name', None) if known else None) or display_name or key.partition('@')[0] or key
        avatar_name = (getattr(contact, 'avatar_name', None) if known else None) or name
        photo = contact_photo(contact) if known else None
        if mangling_enabled():
            name = mangled_name(name, uri=key or None)
            avatar_name = mangled_name(avatar_name, uri=key or None)
            photo = None
        return name, photo, avatar_initials(avatar_name), QColor(avatar_color(avatar_name))

    def name(self, item):
        return self._lookup(item)[0]

    def draw(self, painter, rect, item):
        from blink.messagepane.header import draw_avatar
        name, photo, letters, colour = self._lookup(item)
        draw_avatar(painter, rect, photo, letters, colour)
