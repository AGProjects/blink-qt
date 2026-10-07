"""Composer: where a message is typed, at the bottom of the message pane.

Plain text in the system font (at the pane's font size), growing up to six
lines. Enter sends, Shift+Enter starts a new line. While text is being typed
the peer is told so (is-composing active, renewed every 10 s while typing,
idle when the text is cleared). Pasting inserts plain text; pasted or dropped
files are handed on (filesDropped) to be sent, as are the files chosen from
the paperclip menu (Files..., Screenshot...).
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
        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)
        outer.addLayout(row)
        self.attach_button = QToolButton(self)
        self.attach_button.setAutoRaise(True)
        self.attach_button.setIconSize(QSize(18, 18))
        self.attach_button.setToolTip(translate('message_pane', 'Send files or a screenshot'))
        self.attach_menu = QMenu(self.attach_button)
        self.attach_menu.addAction(translate('message_pane', 'Files…'), self._choose_files)
        self.attach_menu.addAction(translate('message_pane', 'Screenshot…'), self._take_screenshot)
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
        self.attach_button.setIcon(themed_icon(Resources.get('icons/attach.svg'), '#d0d0d0'))

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

    def _send(self):
        text = self.text().strip('\n')
        if not text.strip():
            return
        self._composing_timer.stop()
        self.sendText.emit(text)
        self._loading = True
        self.edit.clear()
        self._loading = False
        self.send_button.setEnabled(False)
