"""TranscriptStrip: the line between the conversation header and the transcript.

On the left, what is loaded ("36 messages, 6 Oct 08:07 – 7 Oct 09:12") and a
note: loading older messages, the beginning of the conversation, or, only once
the user has scrolled up, that scrolling further up loads older messages. On
the right the search field: typing searches the whole conversation in history
(the transcript shows the hits, highlighted).

Clicking the field opens the search: an X appears at its right and the
category chips show (searchActiveChanged). They stay, with or without text,
until the X is pressed (or Escape in an empty field), which clears the search
and closes it again.
"""

from datetime import datetime

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from blink.util import translate
from blink.widgets.color import follow_theme, secondary_text_color
from blink.widgets.labels import ElidedLabel
from blink.widgets.lineedit import SearchBox


__all__ = ['TranscriptStrip', 'range_text']


class MessageSearchBox(SearchBox):
    """The main window's search box (round, the X inside it), whose X stays while the search is
    open, with or without text, and closes the search rather than only clearing the text."""

    closeRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pinned = False
        self.clear_button.clicked.disconnect()
        self.clear_button.clicked.connect(self.closeRequested)
        self.clear_button.setToolTip(translate('message_pane', 'Close the search'))

    def set_pinned(self, pinned):
        self.pinned = pinned
        self.clear_button.setVisible(pinned or bool(self.text()))

    def _SH_TextChanged(self, text):
        self.clear_button.setVisible(self.pinned or bool(text))

    def keyPressEvent(self, event):
        # Escape clears the text; in an empty box it closes the search
        if event.key() == Qt.Key.Key_Escape:
            if self.text():
                self.clear()
            else:
                self.closeRequested.emit()
            return
        super(SearchBox, self).keyPressEvent(event)


def _moment(when, now):
    when = when.astimezone()
    if when.year == now.year:
        return f'{when.day} {when.strftime("%b")} {when:%H:%M}'
    return f'{when.day} {when.strftime("%b")} {when.year} {when:%H:%M}'


def range_text(items, now=None):
    """'N messages, <first> – <last>' for the loaded rows."""
    if not items:
        return translate('message_pane', 'No messages')
    now = now or datetime.now().astimezone()
    count = translate('message_pane', '1 message') if len(items) == 1 else translate('message_pane', '%d messages') % len(items)
    first, last = _moment(items[0].timestamp, now), _moment(items[-1].timestamp, now)
    return f'{count}, {first}' if first == last else f'{count}, {first} – {last}'


class TranscriptStrip(QWidget):
    searchActiveChanged = pyqtSignal(bool)

    search_delay = 300      # ms after the last key press

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = None
        self.view = None
        self.search_active = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 8, 3)
        layout.setSpacing(8)
        self.info_label = ElidedLabel(self)
        self.info_label.setTextFormat(Qt.TextFormat.PlainText)
        self.info_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.info_label, 1)
        self.search_field = MessageSearchBox(self)
        self.search_field.inactiveText = translate('message_pane', 'Search messages')
        self.search_field.setFixedWidth(190)
        self.search_field.installEventFilter(self)
        self.search_field.closeRequested.connect(self.close_search)
        layout.addWidget(self.search_field)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(self.search_delay)
        self._search_timer.timeout.connect(self._search)
        self.search_field.textChanged.connect(lambda text: self._search_timer.start())
        self.search_field.returnPressed.connect(self._search)
        self.apply_theme()
        follow_theme(self)

    def apply_theme(self):
        palette = self.info_label.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, QPalette.ColorRole.WindowText, secondary_text_color(self.palette(), group))
        self.info_label.setPalette(palette)
        font = self.info_label.font()
        if font.pointSizeF() > 0 and not getattr(self, '_font_set', False):
            font.setPointSizeF(max(font.pointSizeF() - 1, 6))
            self.info_label.setFont(font)
            self._font_set = True

    def eventFilter(self, watched, event):
        if watched is self.search_field and event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.set_search_active(True)
        return False

    def set_search_active(self, active):
        if active == self.search_active:
            return
        self.search_active = active
        self.search_field.set_pinned(active)
        self.searchActiveChanged.emit(active)

    def close_search(self):
        """The X: no search text, no category, and the chips go away."""
        self._search_timer.stop()
        self.search_field.blockSignals(True)
        self.search_field.clear()
        self.search_field.blockSignals(False)
        if self.model is not None and self.model.search_text:
            self.model.search('')
        self.search_field.clearFocus()
        self.set_search_active(False)

    def set_conversation(self, model, view):
        if self.model is not None:
            for signal in self._signals(self.model):
                try:
                    signal.disconnect(self.update_text)
                except TypeError:
                    pass
        self.model, self.view = model, view
        for signal in self._signals(model):
            signal.connect(self.update_text)
        self.search_field.blockSignals(True)
        self.search_field.setText(model.search_text)
        self.search_field.blockSignals(False)
        self._search_timer.stop()
        # a conversation left with a search or a category chosen comes back with the search open
        self.set_search_active(bool(model.search_text or model.category))
        self.update_text()

    @staticmethod
    def _signals(model):
        return (model.rowsInserted, model.rowsRemoved, model.modelReset, model.loadingChanged)

    def _search(self):
        self._search_timer.stop()
        if self.model is not None:
            self.model.search(self.search_field.text())

    def update_text(self, *args):
        model = self.model
        if model is None:
            self.info_label.setText('')
            return
        if model.search_text:
            if model.loading:
                text = translate('message_pane', 'Searching…')
            elif not model.items:
                text = translate('message_pane', 'No messages contain “%s”') % model.search_text
            else:
                count = len(model.items)
                text = (translate('message_pane', '1 message contains “%s”') % model.search_text if count == 1
                        else translate('message_pane', '%d messages contain “%s”') % (count, model.search_text))
                if model.search_truncated:
                    text += translate('message_pane', ' (the newest %d)') % count
        else:
            text = range_text(model.items)
            if model.loading:
                note = translate('message_pane', 'loading older messages…') if model.items else translate('message_pane', 'loading…')
            elif model.has_newer:
                note = translate('message_pane', 'newer messages below')
            elif not model.has_more and model.loaded:
                note = translate('message_pane', 'the beginning of the conversation')
            elif self._scrolled_up():
                note = translate('message_pane', 'keep scrolling up for older messages')
            else:
                note = ''
            if note:
                text = f'{text} · {note}'
        self.info_label.setText(text)
        self.info_label.setToolTip(text)

    def _scrolled_up(self):
        if self.view is None:
            return False
        scrollbar = self.view.verticalScrollBar()
        return scrollbar.maximum() > 0 and scrollbar.value() < scrollbar.maximum() - 4
