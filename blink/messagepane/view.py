"""TranscriptView: the list of a conversation's messages in the message pane.

Scrolled per pixel. Scrolling near the top loads the page before (the model's
load_older); the rows inserted above are compensated for, so what was on
screen stays where it was. At the bottom, new messages keep it at the bottom.
Messages are drawn by BubbleDelegate; a click on a link opens it, the context
menu copies a message's text. Behind them the linen texture of Sylk Mobile and
Blink for macOS (dark or light with the theme), tiled from the viewport so the
weave stays still while the transcript scrolls.
"""

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QBrush, QDesktopServices, QGuiApplication, QPalette, QPixmap
from PyQt6.QtWidgets import QAbstractItemView, QFrame, QListView, QMenu

from blink.messagepane.delegate import BubbleDelegate
from blink.messagepane.format import bubble_kind, plain_summary
from blink.util import translate
from blink.resources import Resources
from blink.widgets.color import follow_theme, is_dark_theme


__all__ = ['TranscriptView']


class TranscriptView(QListView):
    load_margin = 48        # pixels from the top that load the page before

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

    def mouseMoveEvent(self, event):
        position = event.position().toPoint()
        if self._link_at(position):
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            anchor = self._link_at(event.position().toPoint())
            if anchor:
                QDesktopServices.openUrl(QUrl(anchor))
                return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        index = self.indexAt(event.pos())
        if not index.isValid():
            return
        item = index.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        anchor = self._link_at(event.pos())
        if anchor:
            menu.addAction(translate('message_pane', 'Copy Link'), lambda: QGuiApplication.clipboard().setText(anchor))
        if bubble_kind(item) in ('text', 'note'):
            layout = self.bubble_delegate.layout(index, self.visualRect(index).width(), self.font())
            text = layout.document.toPlainText()
            menu.addAction(translate('message_pane', 'Copy Text'), lambda: QGuiApplication.clipboard().setText(text))
        else:
            summary = plain_summary(item)
            menu.addAction(translate('message_pane', 'Copy Text'), lambda: QGuiApplication.clipboard().setText(summary))
        menu.exec(event.globalPos())

    def follow_bottom(self):
        """Go to the newest message and stay there (after sending)."""
        self._stick = True
        QTimer.singleShot(0, self.scrollToBottom)
