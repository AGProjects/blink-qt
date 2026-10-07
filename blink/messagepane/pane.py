"""MessagePane: the right side of the main window, next to the contact list.

The contact list is the conversation switcher; this shows the conversation.
It follows the selection in the contact list (it never opens because of it):
one contact selected shows that contact's conversation, anything else the
empty state. The header, the transcript and the composer come with the next
patches (docs/messaging/ui-plan.md, B2-B6); until then a conversation is its
name and address.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from blink.logging import MessagingTrace as log
from blink.util import translate
from blink.widgets.color import follow_theme, secondary_text_color


__all__ = ['MessagePane']


class MessagePane(QWidget):
    minimum_width = 320
    default_width = 480

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('message_pane')
        self.setMinimumWidth(self.minimum_width)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)

        self.empty_label = QLabel(translate('message_pane', 'Select a contact to see messages'), self.stack)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setMargin(24)
        self.stack.addWidget(self.empty_label)

        self.conversation_label = QLabel(self.stack)
        self.conversation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.conversation_label.setTextFormat(Qt.TextFormat.PlainText)
        self.conversation_label.setWordWrap(True)
        self.conversation_label.setMargin(24)
        self.stack.addWidget(self.conversation_label)
        self.stack.setCurrentWidget(self.empty_label)

        self.contact = None
        self.uri = None
        self.key = None

        self.apply_theme()
        follow_theme(self)

    def apply_theme(self):
        palette = self.empty_label.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, QPalette.ColorRole.WindowText, secondary_text_color(self.palette(), group))
        self.empty_label.setPalette(palette)

    def show_conversation(self, contact, uri, key):
        """Switch to the conversation with a contact, on one of its addresses (key: its conversation key)."""
        if contact is self.contact and key == self.key:
            return
        self.contact, self.uri, self.key = contact, uri, key
        name = getattr(contact, 'name', '') or str(uri.uri)
        self.conversation_label.setText(f'{name}\n{uri.uri}')
        self.stack.setCurrentWidget(self.conversation_label)
        log.debug(f'Message pane shows the conversation with {key}')

    def clear(self):
        """No conversation: the empty state."""
        if self.contact is None:
            return
        self.contact = self.uri = self.key = None
        self.stack.setCurrentWidget(self.empty_label)
