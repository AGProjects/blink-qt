"""Composer: where a message is typed, at the bottom of the message pane.

Plain text in the system font (at the pane's font size), growing up to six
lines. Enter sends, Shift+Enter starts a new line. While text is being typed
the peer is told so (is-composing active, renewed every 10 s while typing,
idle when the text is cleared). Pasting inserts plain text; pasted or dropped
files are handed on (filesDropped) to be sent, as are the ones from the
paperclip menu (as on Blink for macOS; Grab a Screenshot...,
then Choose Files... and Paste from Clipboard).

In reply mode (set_reply) a line above the text says what is being answered,
with a button to cancel; the next message sent is that reply. In edit mode
(set_editing) the line says a message is being edited and the text is that
message's; sending replaces it.
"""

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QTextOption
from PyQt6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QMenu, QPlainTextEdit, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from blink.resources import Resources, themed_icon
from blink.widgets.color import follow_theme

from blink.util import translate


__all__ = ['Composer']


class ComposerEdit(QPlainTextEdit):
    sendRequested = pyqtSignal()
    filesDropped = pyqtSignal(list)

    max_lines = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(translate('message_pane', 'Write a message'))
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTabChangesFocus(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def _fit(self):
        """As high as its text, from one line up to max_lines."""
        metrics = self.fontMetrics()
        lines = max(1, min(self.max_lines, int(self.document().size().height())))
        margins = self.contentsMargins()
        height = lines * metrics.lineSpacing() + 2 * int(self.document().documentMargin()) + margins.top() + margins.bottom() + 2
        if self.height() != height:
            self.setFixedHeight(height)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.FontChange:
            self._fit()
            composer = self.parent()
            if hasattr(composer, 'apply_theme'):
                composer.apply_theme()      # the clip follows the text size

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier):
            self.sendRequested.emit()
            return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, source):
        return source.hasUrls() or source.hasText() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasUrls() and all(url.isLocalFile() for url in source.urls()):
            self.filesDropped.emit([url.toLocalFile() for url in source.urls()])
            return
        if source.hasText():
            self.insertPlainText(source.text())       # never formatting: messages are plain text


class Composer(QWidget):
    sendText = pyqtSignal(str)
    filesDropped = pyqtSignal(list)
    composing = pyqtSignal(str)         # 'active' / 'idle'

    composing_refresh = 10000           # ms between active indications while typing

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)
        self.reply_bar = QWidget(self)
        reply_row = QHBoxLayout(self.reply_bar)
        reply_row.setContentsMargins(12, 4, 8, 0)
        reply_row.setSpacing(6)
        from blink.widgets.labels import ElidedLabel
        self.reply_label = ElidedLabel(self.reply_bar)
        self.reply_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reply_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        reply_row.addWidget(self.reply_label, 1)
        cancel = QToolButton(self.reply_bar)
        cancel.setAutoRaise(True)
        cancel.setText('✕')
        cancel.setToolTip(translate('message_pane', 'Do not reply (Escape)'))
        cancel.clicked.connect(self.cancel_mode)
        reply_row.addWidget(cancel)
        self.reply_bar.hide()
        outer.addWidget(self.reply_bar)
        self.reply = None
        self.editing = None
        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)
        outer.addLayout(row)
        self.attach_button = QToolButton(self)
        self.attach_button.setAutoRaise(True)
        # a drawn clip, not the 📎 character: without a colour emoji font it is an empty box
        self.attach_button.setToolTip(translate('message_pane', 'Attach something'))
        self.attach_menu = QMenu(self.attach_button)
        self.attach_menu.aboutToShow.connect(self._fill_attach_menu)
        self.attach_button.setMenu(self.attach_menu)
        self.attach_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.attach_button.setStyleSheet('QToolButton::menu-indicator { image: none; }')
        row.addWidget(self.attach_button, 0, Qt.AlignmentFlag.AlignBottom)
        self.edit = ComposerEdit(self)
        row.addWidget(self.edit, 1)
        self.send_button = QToolButton(self)
        self.send_button.setText(translate('message_pane', 'Send'))
        self.send_button.setToolTip(translate('message_pane', 'Send (Enter); Shift+Enter starts a new line'))
        self.send_button.setEnabled(False)
        row.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)

        self._composing_timer = QTimer(self)
        self._composing_timer.setSingleShot(True)
        self._composing_timer.setInterval(self.composing_refresh)
        self._loading = False

        self.edit.sendRequested.connect(self._send)
        self.send_button.clicked.connect(self._send)
        self.edit.filesDropped.connect(self.filesDropped)
        self.edit.textChanged.connect(self._SH_TextChanged)
        self._directory = ''
        self.apply_theme()
        follow_theme(self)

    def apply_theme(self):
        size = self.edit.fontMetrics().height() + 4
        self.attach_button.setIcon(themed_icon(Resources.get('icons/paperclip.svg'), '#bdbdbd'))
        self.attach_button.setIconSize(QSize(size, size))

    def _fill_attach_menu(self):
        """Above the line what does not exist yet (a screenshot), below it what does (files, the clipboard)."""
        from blink.screenshot import PortalScreenshot
        menu = self.attach_menu
        menu.clear()
        grab = menu.addAction(translate('message_pane', 'Grab a Screenshot…'), self._take_screenshot)
        if PortalScreenshot._busy is not None:
            grab.setEnabled(False)
            grab.setToolTip(translate('message_pane', 'A screenshot is already being taken'))
        menu.addSeparator()
        menu.addAction(translate('message_pane', 'Choose Files…'), self._choose_files)
        paste = menu.addAction(translate('message_pane', 'Paste from Clipboard'), self._paste_files)
        paste.setEnabled(self._clipboard_has_files())

    @staticmethod
    def _clipboard_has_files():
        from PyQt6.QtWidgets import QApplication
        data = QApplication.clipboard().mimeData()
        return data is not None and (data.hasImage() or (data.hasUrls() and all(url.isLocalFile() for url in data.urls())))

    def _paste_files(self):
        import os
        import tempfile
        import uuid
        from PyQt6.QtWidgets import QApplication
        data = QApplication.clipboard().mimeData()
        if data is None:
            return
        if data.hasUrls() and all(url.isLocalFile() for url in data.urls()):
            self.filesDropped.emit([url.toLocalFile() for url in data.urls()])
        elif data.hasImage():
            image = QApplication.clipboard().image()
            path = os.path.join(tempfile.gettempdir(), f'blink-clipboard-{uuid.uuid4().hex[:8]}.png')
            if not image.isNull() and image.save(path, 'PNG'):
                self.filesDropped.emit([path])

    def _choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, translate('message_pane', 'Send Files'), self._directory)
        if paths:
            import os
            self._directory = os.path.dirname(paths[0])
            self.filesDropped.emit(paths)

    def _take_screenshot(self):
        from blink.screenshot import PortalScreenshot
        PortalScreenshot.take(lambda path: path and self.filesDropped.emit([path]))

    def text(self):
        return self.edit.toPlainText()

    def set_text(self, text):
        """Show a conversation's unsent text (no typing indication for it)."""
        self._loading = True
        self._composing_timer.stop()
        self.edit.setPlainText(text or '')
        self.edit.moveCursor(self.edit.textCursor().MoveOperation.End)
        self._loading = False
        self.send_button.setEnabled(bool(self.text().strip()))

    def _SH_TextChanged(self):
        has_text = bool(self.text().strip())
        self.send_button.setEnabled(has_text)
        if self._loading:
            return
        if not has_text:
            if self._composing_timer.isActive():
                self._composing_timer.stop()
                self.composing.emit('idle')
        elif not self._composing_timer.isActive():
            self.composing.emit('active')
            self._composing_timer.start()

    def cancel_mode(self):
        if self.editing is not None:
            self.editing = None
            self.set_text(self._draft)
        self.set_reply(None)

    def set_editing(self, editing):
        """Edit mode: editing is {'id', 'text', 'timestamp', 'account_id'} of one's own message."""
        if self.editing is None:
            self._draft = self.text()
        self.reply = None
        self.editing = editing
        self.reply_label.setText(translate('message_pane', 'Editing the message (Escape to leave it as it was)'))
        self.reply_bar.show()
        self.set_text(editing['text'])
        self.edit.setFocus()

    _draft = ''

    def set_reply(self, reply):
        """Reply mode: reply is {'id', 'name', 'text'} of the message answered, or None."""
        if reply is not None and self.editing is not None:
            self.editing = None
            self.set_text(self._draft)
        self.reply = reply
        if reply is None:
            if self.editing is None:
                self.reply_bar.hide()
            return
        text = ' '.join(str(reply.get('text') or '').split())
        self.reply_label.setText(translate('message_pane', 'Replying to %s: %s') % (reply.get('name') or translate('message_pane', 'the message'), text))
        self.reply_bar.show()
        self.edit.setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and (self.reply is not None or self.editing is not None):
            self.cancel_mode()
            return
        super().keyPressEvent(event)

    def _send(self):
        text = self.text().strip('\n')
        if not text.strip():
            return
        self._composing_timer.stop()
        self.sendText.emit(text)       # reads self.reply and self.editing, then they are cleared
        self.editing = None
        self._draft = ''
        self.set_reply(None)
        self._loading = True
        self.edit.clear()
        self._loading = False
        self.send_button.setEnabled(False)
