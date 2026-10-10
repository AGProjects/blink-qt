"""Forward: messages sent again, as new messages, to another conversation.

From a message's menu or the grid's ticked tiles. ForwardDialog offers the
conversations with messages, newest first, then every other addressbook contact
(the current conversation, deleted and blocked contacts left out), with a field
to narrow them by name or address; one or more are chosen. A text goes as a new text; a file as a new
transfer of the file here, with its caption (a label companion) when it has
one, so the other party gets it as if it was sent to them; a file not here yet
is downloaded first and sent when it is in place. Locations, calls and what
cannot be read (an encrypted text without the key) are not forwarded.

The same selector chooses whom to invite to a conference (Join Conference):
title, prompt and button text are given to it.
"""

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QPalette
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem, QStyle,
                             QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout)

from blink.util import translate


__all__ = ['ConversationRowDelegate', 'ForwardDialog', 'NameRole', 'forwardable', 'recent_conversations']


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


def recent_conversations(exclude=None):
    """[(key, name)] of everyone a message can be forwarded to: the conversations with
    messages, newest first, then every other addressbook contact by name (each of its
    addresses), leaving out the conversation the messages come from and the contacts
    in Deleted or Blocked."""
    from blink.history import ConversationPreviews
    from blink.contacts import URIUtils, is_blocked_contact, is_deleted_contact
    times = dict(getattr(ConversationPreviews(), 'message_times', {}) or {})
    keys = sorted((key for key in times if key and key != exclude and times[key] is not None), key=lambda key: times[key], reverse=True)
    result, seen = [], set(keys)
    if exclude:
        seen.add(exclude)
    for key in keys:
        name = ''
        try:
            contact, _ = URIUtils.find_contact(key)
            name = getattr(contact, 'name', '') or ''
        except Exception:
            pass
        result.append((key, name if name and name != key else ''))
    try:
        from sipsimple.account import AccountManager
        from sipsimple.addressbook import AddressbookManager
        from blink.history import conversation_key
        account = AccountManager().default_account
        others = []
        for contact in AddressbookManager().get_contacts():
            if is_deleted_contact(contact) or is_blocked_contact(contact):
                continue
            for contact_uri in contact.uris:
                key = conversation_key(str(contact_uri.uri), account)
                if key and key not in seen:
                    seen.add(key)
                    others.append((key, contact.name if contact.name and contact.name != key else ''))
        others.sort(key=lambda pair: ((pair[1] or pair[0]).lower(), pair[0]))
        result.extend(others)
    except Exception:
        pass
    return result


NameRole = Qt.ItemDataRole.UserRole + 1


class ConversationRowDelegate(QStyledItemDelegate):
    """A conversation in the list: the name in bold, the address under it, smaller and dimmer;
    rows on alternating backgrounds with room around them, so they read as separate entries."""

    padding = 6

    def _fonts(self, option):
        name_font = QFont(option.font)
        name_font.setBold(True)
        address_font = QFont(option.font)
        if address_font.pointSizeF() > 0:
            address_font.setPointSizeF(address_font.pointSizeF() * 0.9)
        return name_font, address_font

    def sizeHint(self, option, index):
        name_font, address_font = self._fonts(option)
        lines = QFontMetrics(name_font).height()
        if index.data(NameRole):
            lines += QFontMetrics(address_font).height() + 1
        return QSize(option.rect.width(), lines + 2 * self.padding)

    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        name, address = index.data(NameRole), index.data(Qt.ItemDataRole.UserRole)
        option.text = ''
        style = option.widget.style() if option.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)       # background, alternation, selection
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        group = QPalette.ColorGroup.Active if option.state & QStyle.StateFlag.State_Active else QPalette.ColorGroup.Inactive
        text_colour = option.palette.color(group, QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        dim_colour = option.palette.color(group, QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.PlaceholderText)
        name_font, address_font = self._fonts(option)
        rect = option.rect.adjusted(self.padding + 4, self.padding, -self.padding, -self.padding)
        painter.save()
        painter.setFont(name_font)
        painter.setPen(text_colour)
        name_height = QFontMetrics(name_font).height()
        painter.drawText(QRect(rect.left(), rect.top(), rect.width(), name_height), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(name_font).elidedText(name or address, Qt.TextElideMode.ElideRight, rect.width()))
        if name:
            painter.setFont(address_font)
            painter.setPen(dim_colour)
            painter.drawText(QRect(rect.left(), rect.top() + name_height + 1, rect.width(), QFontMetrics(address_font).height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(address_font).elidedText(address, Qt.TextElideMode.ElideMiddle, rect.width()))
        painter.restore()


class ForwardDialog(QDialog):
    def __init__(self, count, exclude=None, parent=None, title=None, prompt=None, action=None, action_many=None, selected=()):
        super().__init__(parent)
        self.setWindowTitle(title or translate('forward', 'Forward'))
        self.action = action or translate('forward', 'Forward')
        self.action_many = action_many or translate('forward', 'Forward to %d')
        self.key = None             # the first of keys
        self.keys = []              # the conversations chosen: one or more
        self.names = {}             # key: name, of those chosen
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        if prompt is None:
            prompt = translate('forward', 'Forward 1 message to:') if count == 1 else translate('forward', 'Forward %d messages to:') % count
        label = QLabel(prompt + ' ' + translate('forward', '(click to choose one or more)'), self)
        layout.addWidget(label)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(translate('forward', 'Name or address'))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.list = QListWidget(self)
        self.list.setMinimumSize(340, 300)
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)     # a click chooses or unchooses a row
        self.list.setItemDelegate(ConversationRowDelegate(self.list))
        for key, name in recent_conversations(exclude):
            row = QListWidgetItem(f'{name}\n{key}' if name else key)       # the text is what the search field matches
            row.setData(Qt.ItemDataRole.UserRole, key)
            row.setData(NameRole, name)
            self.list.addItem(row)
            if key in selected:
                row.setSelected(True)
        self.list.itemSelectionChanged.connect(self._update_button)
        self.list.itemDoubleClicked.connect(self._SH_DoubleClicked)
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(self)
        self.forward_button = buttons.addButton(self.action, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._choose)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_button()
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
        # what is chosen stays chosen while the list is narrowed

    def _update_button(self):
        count = len(self.list.selectedItems())
        self.forward_button.setEnabled(count > 0)
        self.forward_button.setText(self.action if count < 2 else self.action_many % count)

    def _SH_DoubleClicked(self, row):
        row.setSelected(True)       # the double click toggled it twice: it is meant
        self._choose()

    def _choose(self):
        rows = [self.list.item(number) for number in range(self.list.count()) if self.list.item(number).isSelected()]
        if not rows:
            return
        self.keys = [row.data(Qt.ItemDataRole.UserRole) for row in rows]
        self.names = {row.data(Qt.ItemDataRole.UserRole): row.data(NameRole) or '' for row in rows}
        self.key = self.keys[0]
        self.accept()
