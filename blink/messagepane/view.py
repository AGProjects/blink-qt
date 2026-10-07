"""TranscriptView: the list of a conversation's messages in the message pane.

Scrolled per pixel. Scrolling near the top loads the page before (the model's
load_older); the rows inserted above are compensated for, so what was on
screen stays where it was. At the bottom, new messages keep it at the bottom.
Messages are drawn by BubbleDelegate; a click on a link opens it. A right
click, or the actions button a bubble shows under the mouse, opens its menu:
copy text or link, open or save the file, and delete (asking first; on one's
own message also for the other party), handed to the pane (actionRequested). Behind them the linen texture of Sylk Mobile and
Blink for macOS (dark or light with the theme), tiled from the viewport so the
weave stays still while the transcript scrolls.
"""

from PyQt6.QtCore import Qt, QPoint, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QDesktopServices, QGuiApplication, QPalette, QPixmap
from PyQt6.QtWidgets import QAbstractItemView, QFrame, QListView, QMenu

from blink.messagepane.delegate import BubbleDelegate
from blink.messagepane.files import local_file
from blink.messagepane.format import bubble_kind, plain_summary
from blink.util import translate
from blink.resources import Resources
from blink.widgets.color import follow_theme, is_dark_theme


__all__ = ['TranscriptView']


class TranscriptView(QListView):
    load_margin = 48        # pixels from the top that load the page before

    actionRequested = pyqtSignal(str, object)      # ('delete', 'reply', 'edit', 'caption', 'info' or 'open', MessageItem)
    quoteClicked = pyqtSignal(object)              # the reply dict of a clicked quote
    audioAction = pyqtSignal(object, str, float)   # MessageItem, 'play' or 'seek', fraction (seek)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setWordWrap(True)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.bubble_delegate = BubbleDelegate(self)
        self.setItemDelegate(self.bubble_delegate)
        self._set_background()
        follow_theme(self)
        from blink.messagepane.media import MediaCache
        MediaCache.instance().ready.connect(self._SH_MediaReady)
        from blink.messagepane.audio import audio_available
        if audio_available():
            from blink.messagepane.audio import AudioInfo, AudioPlayer
            AudioPlayer.instance().changed.connect(self._SH_MediaReady)
            AudioInfo.instance().measured.connect(self._SH_MediaReady)
        self._anchor = None         # (maximum, value) before rows were inserted at the top
        self._stick = True          # follow the bottom
        scrollbar = self.verticalScrollBar()
        scrollbar.valueChanged.connect(self._SH_ScrollValueChanged)
        scrollbar.rangeChanged.connect(self._SH_ScrollRangeChanged)

    def setModel(self, model):
        old = self.model()
        if old is not None:
            for signal, slot in self._connections(old):
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass
        super().setModel(model)
        self._anchor = None
        self._stick = True
        if model is not None:
            for signal, slot in self._connections(model):
                signal.connect(slot)
            QTimer.singleShot(0, self.scrollToBottom)

    def _connections(self, model):
        return ((model.rowsAboutToBeInserted, self._SH_RowsAboutToBeInserted), (model.initialLoadFinished, self._SH_InitialLoadFinished),
                (model.jumped, self._SH_Jumped))

    def _SH_Jumped(self, row):
        """After a jump to a day: its first message at the top."""
        self._stick = False
        self._anchor = None
        index = self.model().index(row, 0)
        QTimer.singleShot(0, lambda: self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtTop))
        QTimer.singleShot(0, self._fill)

    def _at_bottom(self):
        scrollbar = self.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - 4

    def _SH_InitialLoadFinished(self):
        self._stick = True
        QTimer.singleShot(0, self.scrollToBottom)
        QTimer.singleShot(0, self._fill)

    def _SH_RowsAboutToBeInserted(self, parent, first, last):
        model = self.model()
        scrollbar = self.verticalScrollBar()
        if first == 0 and model.rowCount() > 0:
            self._anchor = (scrollbar.maximum(), scrollbar.value())
        self._stick = self._at_bottom() and not model.has_newer

    def _SH_ScrollRangeChanged(self, minimum, maximum):
        scrollbar = self.verticalScrollBar()
        if self._anchor is not None:
            old_maximum, old_value = self._anchor
            self._anchor = None
            scrollbar.setValue(old_value + maximum - old_maximum)
        elif self._stick:
            scrollbar.setValue(maximum)
        self._fill()

    def _fill(self):
        """A page that does not fill the view has no scroll bar to scroll up with: load the one before."""
        model = self.model()
        if model is not None and not model.loading and self.verticalScrollBar().maximum() == 0:
            if model.has_more:
                QTimer.singleShot(0, model.load_older)
            elif model.has_newer:
                QTimer.singleShot(0, model.load_newer)

    def _SH_ScrollValueChanged(self, value):
        model = self.model()
        self._stick = self._at_bottom() and not (model is not None and model.has_newer)
        if model is not None and model.has_newer and not model.loading and value >= self.verticalScrollBar().maximum() - self.load_margin:
            model.load_newer()
        if model is not None and value <= self.load_margin and model.has_more and not model.loading and self.verticalScrollBar().maximum() > 0:
            model.load_older()

    _linen = {}         # dark: QPixmap

    def _set_background(self):
        dark = is_dark_theme()
        pixmap = self._linen.get(dark)
        if pixmap is None:
            pixmap = self._linen[dark] = QPixmap(Resources.get('icons/dark_linen.png' if dark else 'icons/light_linen.png'))
        viewport = self.viewport()
        palette = viewport.palette()
        if not pixmap.isNull():
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
                palette.setBrush(group, QPalette.ColorRole.Base, QBrush(pixmap))
        viewport.setPalette(palette)
        viewport.setAutoFillBackground(True)
        viewport.setBackgroundRole(QPalette.ColorRole.Base)

    def apply_theme(self):
        self._set_background()
        self.bubble_delegate.clear_cache()
        self.scheduleDelayedItemsLayout()
        self.viewport().update()

    def changeEvent(self, event):
        if event.type() in (event.Type.FontChange, event.Type.StyleChange):
            self.bubble_delegate.clear_cache()
        super().changeEvent(event)

    # Links and copying

    def _link_at(self, position):
        index = self.indexAt(position)
        if not index.isValid():
            return ''
        return self.bubble_delegate.anchor_at(index, self.visualRect(index), position)

    def _on_actions_button(self, position):
        index = self.indexAt(position)
        return index.isValid() and self.bubble_delegate.actions_at(index, self.visualRect(index), position)

    def mouseMoveEvent(self, event):
        position = event.position().toPoint()
        if event.buttons() & Qt.MouseButton.LeftButton:
            index = self.indexAt(position)
            hit = self.bubble_delegate.audio_hit(index, self.visualRect(index), position) if index.isValid() else None
            if hit is not None and hit[0] == 'seek':
                self.audioAction.emit(index.data(Qt.ItemDataRole.UserRole), 'seek', hit[1])     # dragging along the waveform
                return
        index = self.indexAt(position)
        on_quote = index.isValid() and self.bubble_delegate.quote_at(index, self.visualRect(index), position) is not None
        if self._link_at(position) or self._on_actions_button(position) or on_quote:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            position = event.position().toPoint()
            if self._on_actions_button(position):
                index = self.indexAt(position)
                rect = self.visualRect(index)
                self._show_menu(index, position, self.viewport().mapToGlobal(position + QPoint(0, 12)))
                return
            index = self.indexAt(position)
            reply = self.bubble_delegate.quote_at(index, self.visualRect(index), position) if index.isValid() else None
            if reply is not None:
                self.quoteClicked.emit(reply)
                return
            anchor = self._link_at(position)
            if anchor:
                QDesktopServices.openUrl(QUrl(anchor))
                return
            if index.isValid():
                item = index.data(Qt.ItemDataRole.UserRole)
                hit = self.bubble_delegate.audio_hit(index, self.visualRect(index), position)
                if hit is not None:
                    self.audioAction.emit(item, hit[0], hit[1] if hit[1] is not None else -1.0)
                    return
                if item is not None and item.category in ('image', 'audio', 'video', 'other'):
                    self.actionRequested.emit('open', item)      # a file: open it, or fetch it
                    return
                if item is not None and item.category == 'call':
                    self.actionRequested.emit('call_details', item)
                    return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        index = self.indexAt(event.pos())
        if not index.isValid():
            return
        self._show_menu(index, event.pos(), event.globalPos())

    def _show_menu(self, index, position, global_position):
        item = index.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        anchor = self._link_at(position)
        if anchor:
            menu.addAction(translate('message_pane', 'Copy Link'), lambda: QGuiApplication.clipboard().setText(anchor))
        if bubble_kind(item) in ('text', 'note'):
            layout = self.bubble_delegate.layout(index, self.visualRect(index).width(), self.font())
            text = layout.document.toPlainText()
            menu.addAction(translate('message_pane', 'Copy Text'), lambda: QGuiApplication.clipboard().setText(text))
        else:
            summary = plain_summary(item)
            menu.addAction(translate('message_pane', 'Copy Text'), lambda: QGuiApplication.clipboard().setText(summary))
        path = local_file(item)
        if path:
            menu.addSeparator()
            menu.addAction(translate('message_pane', 'Open'), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
            menu.addAction(translate('message_pane', 'Save As…'), lambda: self._save_as(path))
        if bubble_kind(item) != 'note':
            menu.addSeparator()
            menu.addAction(translate('message_pane', 'Reply'), lambda: self.actionRequested.emit('reply', item))
            if item.outgoing and bubble_kind(item) == 'text':
                menu.addAction(translate('message_pane', 'Edit'), lambda: self.actionRequested.emit('edit', item))
            if item.outgoing and item.category in ('image', 'video'):
                menu.addAction(translate('message_pane', 'Edit Caption…'), lambda: self.actionRequested.emit('caption', item))
        menu.addSeparator()
        menu.addAction(translate('message_pane', 'Info…'), lambda: self.actionRequested.emit('info', item))
        delete = menu.addAction(translate('message_pane', 'Delete…'), lambda: self.actionRequested.emit('delete', item))
        delete.setEnabled(bubble_kind(item) != 'note' or item.category is not None)
        menu.exec(global_position)

    def _save_as(self, path):
        import os
        import shutil
        from PyQt6.QtWidgets import QFileDialog
        target, _ = QFileDialog.getSaveFileName(self, translate('message_pane', 'Save As'), os.path.join(os.path.expanduser('~'), os.path.basename(path)))
        if target:
            try:
                shutil.copyfile(path, target)
            except OSError as e:
                from blink.logging import ActivityLog
                ActivityLog().warning(f'[ui] Cannot save {path} as {target}: {e}')

    def follow_bottom(self):
        """Go to the newest message and stay there (after sending)."""
        self._stick = True
        QTimer.singleShot(0, self.scrollToBottom)

    def show_message(self, message_id):
        """Scroll to a loaded message and point it out; False when it is not loaded."""
        model = self.model()
        row = model.row_of(message_id) if model is not None else None
        if row is None:
            return False
        self._stick = False
        self.scrollTo(model.index(row, 0), QAbstractItemView.ScrollHint.PositionAtCenter)
        self.bubble_delegate.flash(message_id)
        return True

    def _SH_MediaReady(self, path):
        """A picture finished decoding: repaint (a bubble that waited for it draws it now)."""
        self.viewport().update()
