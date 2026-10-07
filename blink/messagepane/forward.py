"""Forward: messages sent again, as new messages, to another conversation.

From a message's menu or the grid's ticked tiles. ForwardDialog offers the 12
conversations with the newest messages (the current one left out), with a field
to narrow them by name or address. A text goes as a new text; a file as a new
transfer of the file here, with its caption (a label companion) when it has
one, so the other party gets it as if it was sent to them; a file not here yet
is downloaded first and sent when it is in place. Locations, calls and what
cannot be read (an encrypted text without the key) are not forwarded.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from blink.util import translate


__all__ = ['ForwardDialog', 'forwardable', 'recent_conversations']


FILE_CATEGORIES = ('image', 'video', 'audio', 'other')


def forwardable(item):
    """Whether a message can be forwarded: a readable text, or a file (not one still being sent)."""
    if item is None or getattr(item, 'upload', None) is not None:
        return False
    if item.category in FILE_CATEGORIES:
        return True
    if item.category == 'text':
        from blink.messagepane.format import bubble_kind
        return bubble_kind(item) == 'text'
    return False


def recent_conversations(exclude=None, count=12):
    """[(key, name)] of the conversations with the newest messages, newest first."""
    from blink.history import ConversationPreviews
    times = dict(getattr(ConversationPreviews(), 'message_times', {}) or {})
    keys = sorted((key for key in times if key and key != exclude and times[key] is not None), key=lambda key: times[key], reverse=True)[:count]
    result = []
    for key in keys:
        name = ''
        try:
            from blink.contacts import URIUtils
            contact, _ = URIUtils.find_contact(key)
            name = getattr(contact, 'name', '') or ''
        except Exception:
            pass
        result.append((key, name if name and name != key else ''))
    return result


class ForwardDialog(QDialog):
    def __init__(self, count, exclude=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate('forward', 'Forward'))
        self.key = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        label = QLabel(translate('forward', 'Forward 1 message to:') if count == 1 else translate('forward', 'Forward %d messages to:') % count, self)
        layout.addWidget(label)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(translate('forward', 'Name or address'))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.list = QListWidget(self)
        self.list.setMinimumSize(340, 300)
        for key, name in recent_conversations(exclude):
            row = QListWidgetItem(f'{name}\n{key}' if name else key)
            row.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(row)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda row: self._choose())
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(self)
        self.forward_button = buttons.addButton(translate('forward', 'Forward'), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._choose)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.forward_button.setEnabled(self.list.count() > 0)
        self.search.setFocus()

    def _filter(self, text):
        text = text.strip().lower()
        first = None
        for number in range(self.list.count()):
            row = self.list.item(number)
            hidden = bool(text) and text not in row.text().lower()
            row.setHidden(hidden)
            if not hidden and first is None:
                first = row
        if first is not None:
            self.list.setCurrentItem(first)
        self.forward_button.setEnabled(first is not None)

    def _choose(self):
        row = self.list.currentItem()
        if row is None or row.isHidden():
            return
        self.key = row.data(Qt.ItemDataRole.UserRole)
        self.accept()
