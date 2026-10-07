"""MessagePane: the right side of the main window, next to the contact list.

The contact list is the conversation switcher; this shows the conversation.
For now it holds the empty state only: the header, the transcript and the
composer come with the next patches (docs/messaging/ui-plan.md, B2-B6).
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

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
        self.stack.setCurrentWidget(self.empty_label)

        self.apply_theme()
        follow_theme(self)

    def apply_theme(self):
        palette = self.empty_label.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, QPalette.ColorRole.WindowText, secondary_text_color(self.palette(), group))
        self.empty_label.setPalette(palette)
