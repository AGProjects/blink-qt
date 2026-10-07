"""GridView: pictures, videos and locations as tiles, when the filter shows one of them.

The same ConversationModel as the transcript (the category it shows), drawn as
tiles of 4:3, centre-cropped, in 2 to 6 columns (3 at first, kept across
restarts), oldest at the top, under a divider for each month; it opens at the
newest and loads older months as it is scrolled up, as the transcript does.
A picture or a movie's poster fills its tile (MediaCache, off the GUI thread),
a movie has the play badge and its length, a location its map
(blink.messagepane.locations); a file not here yet says what it is and how far
its download got. The file's size is on a pill at the bottom left, the info
button at the top right. A click opens the file (or fetches it), a location
its map window; the context menu has Open, Save As, Info and Delete.
"""

import math
import os

from datetime import datetime

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QAbstractScrollArea, QMenu

from blink.util import translate
from blink.widgets.color import is_dark_theme, secondary_text_color


__all__ = ['GridView', 'GRID_CATEGORIES']


GRID_CATEGORIES = ('image', 'video', 'location')


class GridView(QAbstractScrollArea):
    actionRequested = pyqtSignal(str, object)      # as TranscriptView's: 'open', 'info', 'delete', 'location', MessageItem

    margin = 10
    gap = 4
    divider_height = 30
    min_columns, max_columns = 2, 6
    load_margin = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = None
        self.columns = max(self.min_columns, min(self.max_columns, int(QSettings().value('message_pane/grid_columns', 3) or 3)))
        self.progress_of = None         # callable(message id) -> download fraction or None
        self._entries = []              # ('divider', QRectF, text) or ('tile', QRectF, row)
        self._height = 0
        self._paths = {}
        self._anchor = None             # distance from the bottom before rows were inserted at the top
        self._stick = True
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setMouseTracking(True)
        self.verticalScrollBar().valueChanged.connect(self._SH_Scrolled)
        self.verticalScrollBar().setSingleStep(40)
        from blink.messagepane.media import MediaCache
        MediaCache.instance().ready.connect(self.viewport().update)
        from blink.messagepane.locations import LocationStore, TileCache
        TileCache.instance().tileReady.connect(self.viewport().update)
        LocationStore.instance().changed.connect(lambda *args: self.viewport().update())
        from blink.messagepane.video import VideoProbe, video_available
        if video_available():
            VideoProbe.instance().probed.connect(lambda *args: self.viewport().update())

    # Model

    def set_model(self, model):
        if self.model is not None:
            for signal, slot in self._connections(self.model):
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass
        self.model = model
        self._paths = {}
        self._stick = True
        if model is not None:
            for signal, slot in self._connections(model):
                signal.connect(slot)
        self._relayout()

    def _connections(self, model):
        return ((model.modelReset, self._SH_Reset), (model.rowsAboutToBeInserted, self._SH_AboutToInsert), (model.rowsInserted, self._relayout),
                (model.rowsRemoved, self._relayout), (model.dataChanged, self._SH_DataChanged), (model.layoutChanged, self._relayout))

    def _SH_Reset(self):
        self._paths = {}
        self._stick = True
        self._relayout()

    def _SH_AboutToInsert(self, parent, first, last):
        scrollbar = self.verticalScrollBar()
        if first == 0 and self.model.rowCount() > 0:
            self._anchor = scrollbar.maximum() - scrollbar.value()

    def _SH_DataChanged(self, *args):
        self.viewport().update()

    def forget(self, message_id):
        """A download changed where a message's file is."""
        self._paths.pop(message_id, None)
        self.viewport().update()

    def set_columns(self, columns):
        columns = max(self.min_columns, min(self.max_columns, columns))
        if columns != self.columns:
            self.columns = columns
            QSettings().setValue('message_pane/grid_columns', columns)
            self._relayout()

    # Layout

    def _tile_size(self):
        width = (self.viewport().width() - 2 * self.margin - (self.columns - 1) * self.gap) / self.columns
        return QSize(max(40, int(width)), max(30, int(width * 3 / 4)))

    def _relayout(self, *args):
        entries = []
        y = 0
        if self.model is not None and self.model.items:
            size = self._tile_size()
            month, column = None, 0
            for row, item in enumerate(self.model.items):
                when = item.timestamp.astimezone()
                if (when.year, when.month) != month:
                    if month is not None and column:
                        y += size.height() + self.gap
                    month, column = (when.year, when.month), 0
                    entries.append(('divider', QRectF(0, y, self.viewport().width(), self.divider_height), self._month_text(when)))
                    y += self.divider_height
                x = self.margin + column * (size.width() + self.gap)
                entries.append(('tile', QRectF(x, y, size.width(), size.height()), row))
                column += 1
                if column == self.columns:
                    column = 0
                    y += size.height() + self.gap
            if column:
                y += size.height() + self.gap
            y += self.margin
        self._entries = entries
        self._height = y
        scrollbar = self.verticalScrollBar()
        scrollbar.setPageStep(self.viewport().height())
        scrollbar.setRange(0, max(0, int(y - self.viewport().height())))
        if self._anchor is not None:
            scrollbar.setValue(scrollbar.maximum() - self._anchor)
            self._anchor = None
        elif self._stick:
            scrollbar.setValue(scrollbar.maximum())
        self.viewport().update()
        QTimer.singleShot(0, self._fill)

    @staticmethod
    def _month_text(when):
        today = datetime.now().astimezone()
        name = when.strftime('%B')
        return name if when.year == today.year else f'{name} {when.year}'

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def _SH_Scrolled(self, value):
        scrollbar = self.verticalScrollBar()
        self._stick = value >= scrollbar.maximum() - 4
        model = self.model
        if model is not None and value <= self.load_margin and model.has_more and not model.loading:
            model.load_older()
        if model is not None and model.has_newer and not model.loading and value >= scrollbar.maximum() - self.load_margin:
            model.load_newer()
        self.viewport().update()

    def _fill(self):
        """A page that does not fill the view has nothing to scroll: load the one before."""
        model = self.model
        if model is not None and not model.loading and self.verticalScrollBar().maximum() == 0 and model.has_more:
            model.load_older()

    def visible_items(self):
        if self.model is None or not self.isVisible():
            return []
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        items = self.model.items
        return [items[row] for kind, rect, row in self._entries if kind == 'tile' and rect.bottom() >= top and rect.top() <= bottom and row < len(items)]

    def _tile_at(self, position):
        point = QPointF(position) + QPointF(0, self.verticalScrollBar().value())
        for kind, rect, row in self._entries:
            if kind == 'tile' and rect.contains(point) and self.model is not None and row < len(self.model.items):
                return self.model.items[row], rect.translated(0, -self.verticalScrollBar().value())
        return None, None

    # Painting

    def _path(self, item):
        try:
            return self._paths[item.id]
        except KeyError:
            from blink.messagepane.files import local_file
            path = self._paths[item.id] = local_file(item)
            return path

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        offset = self.verticalScrollBar().value()
        visible = QRectF(self.viewport().rect()).translated(0, offset)
        secondary = secondary_text_color(self.palette())
        small = self.font()
        if small.pointSizeF() > 0:
            small.setPointSizeF(max(small.pointSizeF() - 1.5, 6))
        items = self.model.items if self.model is not None else []
        for kind, rect, value in self._entries:
            if not rect.intersects(visible):
                continue
            target = rect.translated(0, -offset)
            if kind == 'divider':
                self._paint_divider(painter, target, value, small, secondary)
            elif value < len(items):
                self._paint_tile(painter, target, items[value], small)

    def _paint_divider(self, painter, rect, text, font, colour):
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        painter.setPen(colour)
        width = metrics.horizontalAdvance(text) + 16
        middle = rect.center().y()
        painter.drawLine(QPointF(rect.left() + self.margin, middle), QPointF(rect.center().x() - width / 2, middle))
        painter.drawLine(QPointF(rect.center().x() + width / 2, middle), QPointF(rect.right() - self.margin, middle))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_tile(self, painter, rect, item, small):
        clip = QPainterPath()
        clip.addRoundedRect(rect, 6, 6)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(rect, QColor(255, 255, 255, 24) if is_dark_theme() else QColor(0, 0, 0, 28))
        drawn = False
        if item.category == 'location':
            from blink.messagepane.locations import LocationStore, fit_view, paint_map
            share = LocationStore.instance().shown(item)
            if share:
                track = [(lat, lng) for lat, lng, _ in share['track']]
                zoom, centre = fit_view(track + ([share['destination']] if share['destination'] else []), rect.size().toSize(), padding=16)
                paint_map(painter, rect, zoom, centre, track=track, pin=track[-1] if track else None, destination=share['destination'], start=track[0] if track else None)
                drawn = True
        else:
            path = self._path(item)
            if path:
                drawn = self._paint_cover(painter, rect, path)
        if not drawn:
            self._paint_placeholder(painter, rect, item, small)
        painter.restore()
        if item.category == 'video' and drawn:
            self._paint_video_marks(painter, rect, item, small)
        self._paint_size(painter, rect, item, small)
        self._paint_info_button(painter, self._info_rect(rect))

    def _paint_cover(self, painter, rect, path):
        """The picture (or a movie's poster) filling rect, its middle kept."""
        from blink.messagepane.media import MediaCache
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        pixmap = MediaCache.instance().thumbnail(path, (rect.width() * ratio * 2, rect.height() * ratio * 2))
        if pixmap is None or pixmap.isNull():
            return False
        source = QRectF(pixmap.rect())
        aspect = rect.width() / rect.height()
        if source.width() / source.height() > aspect:
            width = source.height() * aspect
            source = QRectF(source.left() + (source.width() - width) / 2, source.top(), width, source.height())
        else:
            height = source.width() / aspect
            source = QRectF(source.left(), source.top() + (source.height() - height) / 2, source.width(), height)
        painter.drawPixmap(rect, pixmap, source)
        return True

    def _paint_placeholder(self, painter, rect, item, font):
        from blink.messagepane.files import file_info
        info = file_info(item) or {}
        what = {'image': translate('message_pane', 'Picture'), 'video': translate('message_pane', 'Video'), 'location': translate('message_pane', 'Location')}.get(item.category, '')
        progress = self.progress_of(item.id) if self.progress_of is not None else None
        lines = [info.get('name') or what]
        if progress is not None and progress < 1:
            lines.append(translate('message_pane', 'downloading %d%%') % int(progress * 100))
        painter.setFont(font)
        painter.setPen(secondary_text_color(self.palette()))
        metrics = QFontMetricsF(font)
        text = '\n'.join(metrics.elidedText(line, Qt.TextElideMode.ElideMiddle, rect.width() - 12) for line in lines)
        painter.drawText(rect.adjusted(6, 6, -6, -6), Qt.AlignmentFlag.AlignCenter, text)

    def _paint_video_marks(self, painter, rect, item, font):
        from blink.messagepane.format import format_clock
        from blink.messagepane.video import VideoProbe, video_available
        size = max(22.0, min(44.0, min(rect.width(), rect.height()) * 0.28))
        disc = QRectF(rect.center().x() - size / 2, rect.center().y() - size / 2, size, size)
        circle = QPainterPath()
        circle.addEllipse(disc)
        painter.save()
        painter.fillPath(circle, QColor(0, 0, 0, 130))
        painter.setPen(QColor(255, 255, 255, 160))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(disc)
        from PyQt6.QtGui import QPolygonF
        centre, side = disc.center() + QPointF(size * 0.04, 0), size * 0.3
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#ffffff'))
        painter.drawPolygon(QPolygonF([QPointF(centre.x() - side * 0.4, centre.y() - side * 0.6), QPointF(centre.x() - side * 0.4, centre.y() + side * 0.6),
                                       QPointF(centre.x() + side * 0.65, centre.y())]))
        painter.restore()
        path = self._path(item)
        info = VideoProbe.instance().info(path) if path and video_available() else None
        if info and info.get('duration'):
            self._pill(painter, format_clock(info['duration']), rect, font, right=True)

    def _paint_size(self, painter, rect, item, font):
        if item.category == 'location':
            return
        from blink.messagepane.files import file_info
        from blink.messagepane.format import format_size
        info = file_info(item) or {}
        size = info.get('size')
        if not size:
            path = self._path(item)
            size = os.path.getsize(path) if path and os.path.exists(path) else None
        text = format_size(size) if size else ''
        if text:
            self._pill(painter, text, rect, font, right=False)

    def _pill(self, painter, text, rect, font, right):
        metrics = QFontMetricsF(font)
        pill = QRectF(0, 0, metrics.horizontalAdvance(text) + 10, metrics.height() + 2)
        if pill.width() > rect.width() - 8:
            return
        if right:
            pill.moveBottomRight(rect.bottomRight() - QPointF(4, 4))
        else:
            pill.moveBottomLeft(rect.bottomLeft() + QPointF(4, -4))
        path = QPainterPath()
        path.addRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.fillPath(path, QColor(0, 0, 0, 120))
        painter.setFont(font)
        painter.setPen(QColor('#ffffff'))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _info_rect(rect):
        return QRectF(rect.right() - 24, rect.top() + 4, 20, 20)

    def _paint_info_button(self, painter, rect):
        painter.save()
        painter.setPen(QPen(QColor(255, 255, 255, 220), 1.2))
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawEllipse(rect)
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QColor('#ffffff'))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, 'i')
        painter.restore()

    # Mouse

    def mouseMoveEvent(self, event):
        item, rect = self._tile_at(event.position().toPoint())
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if item is not None else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        item, rect = self._tile_at(event.position().toPoint())
        if item is None:
            return
        if self._info_rect(rect).contains(event.position()):
            self.actionRequested.emit('info', item)
        elif item.category == 'location':
            self.actionRequested.emit('location', item)
        else:
            self.actionRequested.emit('open', item)

    def contextMenuEvent(self, event):
        item, rect = self._tile_at(event.pos())
        if item is None:
            return
        menu = QMenu(self)
        if item.category == 'location':
            menu.addAction(translate('message_pane', 'Show Map'), lambda: self.actionRequested.emit('location', item))
        else:
            path = self._path(item)
            menu.addAction(translate('message_pane', 'Open') if path else translate('message_pane', 'Download'), lambda: self.actionRequested.emit('open', item))
            if path:
                menu.addAction(translate('message_pane', 'Save As…'), lambda: self._save_as(path))
        menu.addSeparator()
        menu.addAction(translate('message_pane', 'Info…'), lambda: self.actionRequested.emit('info', item))
        menu.addAction(translate('message_pane', 'Delete…'), lambda: self.actionRequested.emit('delete', item))
        menu.exec(event.globalPos())

    def _save_as(self, path):
        import shutil
        from PyQt6.QtWidgets import QFileDialog
        target, _ = QFileDialog.getSaveFileName(self, translate('message_pane', 'Save As'), os.path.join(os.path.expanduser('~'), os.path.basename(path)))
        if target:
            try:
                shutil.copyfile(path, target)
            except OSError as e:
                from blink.logging import ActivityLog
                ActivityLog().warning(f'[ui] Cannot save {path} as {target}: {e}')
