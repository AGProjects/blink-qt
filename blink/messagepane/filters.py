"""FilterBar: the categories of a conversation, as chips under the strip.

All, then one chip per kind of message the conversation has (Pictures, Videos,
Audio, Files, Locations, Calls, Texts, Links: texts with a link), from history
(MessageHistory.present_categories, in the db thread). Shown when there are at
least two kinds, so a conversation of texts only has no bar. Choosing a chip
shows that kind only (ConversationModel.set_category), paged from history like
the whole conversation; All shows everything again. The choice is the model's,
so going back to a conversation finds it as it was left.
"""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QSizePolicy, QToolButton, QWidget

from sipsimple.threading import run_in_thread

from blink.logging import MessagingTrace as log
from blink.util import call_in_gui_thread, translate
from blink.widgets.color import follow_theme


__all__ = ['FilterBar', 'CATEGORY_LABELS']


# In the order the chips are shown.
CATEGORY_LABELS = (('image', 'Pictures'), ('video', 'Videos'), ('audio', 'Audio'), ('other', 'Files'), ('location', 'Locations'),
                   ('call', 'Calls'), ('text', 'Texts'), ('links', 'Links'))


class FilterBar(QWidget):
    categoryChosen = pyqtSignal(object)     # a category, or None for all

    refresh_delay = 1000        # ms: new messages may bring a new kind

    def __init__(self, parent=None):
        super().__init__(parent)
        self.key = None
        self.model = None
        self.present = set()
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(10, 2, 8, 4)
        self._layout.setSpacing(6)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self._SH_Clicked)
        self._buttons = {}
        self._extras = []           # widgets the grid adds at the right (blink.messagepane.grid)
        self._stretch_added = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.refresh_delay)
        self._timer.timeout.connect(self.refresh)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.hide()
        self.apply_theme()
        follow_theme(self)

    def apply_theme(self):
        self.setStyleSheet('QToolButton#chip { border: 1px solid palette(mid); border-radius: 10px; padding: 1px 10px; }'
                           'QToolButton#chip:checked { background: palette(highlight); color: palette(highlighted-text); border-color: palette(highlight); }')

    def add_extra(self, widget):
        """A widget at the right of the chips (the grid switch)."""
        self._extras.append(widget)
        self._rebuild()

    def set_conversation(self, model):
        self.model = model
        self.key = model.key if model is not None else None
        self.present = set()
        self._rebuild()
        if model is not None:
            self.refresh()

    def refresh_later(self):
        self._timer.start()

    def refresh(self):
        if self.key is not None:
            self._load(self.key)

    @run_in_thread('db')
    def _load(self, key):
        from blink.history import MessageHistory
        try:
            present = MessageHistory().present_categories(key)
        except Exception as e:
            log.warning(f'Cannot read the kinds of messages with {key}: {e!r}')
            return
        call_in_gui_thread(self._loaded, key, present)

    def _loaded(self, key, present):
        if key != self.key or present == self.present:
            return
        self.present = present
        self._rebuild()

    def _rebuild(self):
        for button in self._buttons.values():
            self._group.removeButton(button)
            button.deleteLater()
        self._buttons = {}
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None and item.widget() not in self._extras:
                item.widget().deleteLater()
        current = self.model.category if self.model is not None else None
        shown = [(category, label) for category, label in CATEGORY_LABELS if category in self.present]
        if current is not None and current not in self.present:
            shown.append((current, dict(CATEGORY_LABELS).get(current, current)))        # the one chosen stays while it is chosen
        chips = [(None, 'All')] + shown
        for number, (category, label) in enumerate(chips):
            button = QToolButton(self)
            button.setObjectName('chip')
            button.setText(translate('message_pane', label))
            button.setCheckable(True)
            button.setChecked(category == current)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty('category', category)
            self._group.addButton(button, number)
            self._buttons[category] = button
            self._layout.addWidget(button)
        self._layout.addStretch(1)
        for widget in self._extras:
            self._layout.addWidget(widget)
        self.setVisible(len(shown) >= 2 or current is not None)

    def _SH_Clicked(self, number):
        button = self._group.button(number)
        category = button.property('category') if button is not None else None
        if self.model is not None:
            self.model.set_category(category)
        self.categoryChosen.emit(category)
