"""BubbleDelegate: how the transcript draws a message.

Incoming messages sit on the left under the peer's avatar, outgoing ones on
the right. Messages of one turn (same direction, same day, less than five
minutes apart) are grouped: the avatar and the sender's name only at the start
of the turn, and less space between them. Text is laid out with QTextDocument:
HTML a peer sent is sanitised, plain text is escaped and its links made
clickable. Texts a client writes on its own are centred notes; an encrypted
body says so; anything else is summarised in a bubble until it gets a bubble
of its own (docs/messaging/ui-plan.md, B4). The first message of a day has the
day above it (Today, Yesterday, the weekday, the date); an outgoing message
shows its delivery state after its time and a failed one is drawn in red.
Layouts are cached per message, width and font.
"""

from datetime import date, timedelta

from PyQt6.QtCore import Qt, QLocale, QPointF, QRectF, QSize, QSizeF
from PyQt6.QtGui import QAbstractTextDocumentLayout, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPalette, QTextDocument, QTextOption
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate

from blink.messagepane.format import bubble_kind, day_label, delivery_mark, linkify, plain_summary, sanitize_html
from blink.widgets.color import is_dark_theme, secondary_text_color


__all__ = ['BubbleDelegate']


class BubbleLayout(object):
    __slots__ = ('kind', 'run_start', 'day_text', 'document', 'text_size', 'bubble_size', 'size', 'time_text', 'mark', 'mark_kind', 'name_text')


class BubbleDelegate(QStyledItemDelegate):
    margin = 10             # left and right of the transcript
    avatar_size = 28
    avatar_gap = 6
    padding_h = 10
    padding_v = 6
    radius = 12
    run_gap = 10            # space above the first message of a turn
    inner_gap = 2           # space between messages of a turn
    turn_interval = timedelta(minutes=5)
    max_bubble_width = 560
    divider_height = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.peer_avatar = None         # callable(painter, rect): draws the peer's avatar
        self._cache = {}

    def clear_cache(self):
        self._cache.clear()

    # Layout

    @staticmethod
    def _item(index):
        return index.data(Qt.ItemDataRole.UserRole)

    def _neighbour(self, index, offset):
        row = index.row() + offset
        model = index.model()
        if 0 <= row < model.rowCount():
            return self._item(model.index(row, 0))
        return None

    def is_run_start(self, index):
        item, previous = self._item(index), self._neighbour(index, -1)
        if previous is None or previous.direction != item.direction:
            return True
        if bubble_kind(previous) == 'note':
            return True
        if previous.timestamp.astimezone().date() != item.timestamp.astimezone().date():
            return True
        return item.timestamp - previous.timestamp > self.turn_interval

    def day_text(self, index):
        """The divider label when this is the first message of its day, else ''."""
        item, previous = self._item(index), self._neighbour(index, -1)
        day = item.timestamp.astimezone().date()
        if previous is not None and previous.timestamp.astimezone().date() == day:
            return ''
        locale = QLocale()
        return day_label(day, date.today(), lambda number: locale.dayName(number, QLocale.FormatType.LongFormat),
                         lambda number: locale.monthName(number, QLocale.FormatType.LongFormat))

    def _small_font(self, font):
        small = QFont(font)
        if small.pointSizeF() > 0:
            small.setPointSizeF(max(small.pointSizeF() - 1.5, 6))
        return small

    def _bubble_width_limit(self, width):
        return max(120, min(self.max_bubble_width, int((width - 2 * self.margin - self.avatar_size - self.avatar_gap) * 0.78)))

    def layout(self, index, width, font):
        item = self._item(index)
        run_start = self.is_run_start(index)
        day_text = self.day_text(index)
        key = (item.id, item.state, item.content_type, len(item.content or ''), width, font.key(), run_start, day_text)
        layout = self._cache.get(key)
        if layout is not None:
            return layout
        if len(self._cache) > 4000:
            self._cache.clear()
        layout = BubbleLayout()
        layout.kind = kind = bubble_kind(item)
        layout.run_start = run_start
        layout.day_text = day_text
        layout.time_text = item.timestamp.astimezone().strftime('%H:%M')
        layout.mark, layout.mark_kind = delivery_mark(item)
        layout.name_text = (item.display_name or '') if run_start and not item.outgoing and kind != 'note' else ''
        small = self._small_font(font)
        document = QTextDocument()
        document.setDocumentMargin(0)
        if kind == 'note':
            document.setDefaultFont(small)
            option = QTextOption(Qt.AlignmentFlag.AlignHCenter)
            option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            document.setDefaultTextOption(option)
            document.setPlainText(plain_summary(item))
            document.setTextWidth(width - 2 * self.margin - 40)
        else:
            document.setDefaultFont(font)
            option = QTextOption()
            option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            document.setDefaultTextOption(option)
            if kind == 'text':
                content = item.content if isinstance(item.content, str) else (item.content or b'').decode('utf-8', 'replace')
                document.setHtml(sanitize_html(content) if item.content_type == 'text/html' else linkify(content))
            elif kind == 'encrypted':
                document.setPlainText('🔒 ' + _('Encrypted message'))
            else:
                document.setPlainText(plain_summary(item))
            limit = self._bubble_width_limit(width) - 2 * self.padding_h
            document.setTextWidth(limit)
            ideal = document.idealWidth()
            if ideal < limit:
                document.setTextWidth(max(ideal, 1))
        layout.document = document
        text_size = document.size()
        layout.text_size = QSizeF(text_size.width(), text_size.height())
        if kind == 'note':
            layout.bubble_size = QSizeF(text_size.width(), text_size.height())
            height = text_size.height() + 2 * self.padding_v
        else:
            time_metrics = QFontMetricsF(small)
            time_width = time_metrics.horizontalAdvance(layout.time_text + ('  ' + layout.mark if layout.mark else ''))
            bubble_width = max(text_size.width(), time_width) + 2 * self.padding_h
            bubble_height = text_size.height() + time_metrics.height() + 2 * self.padding_v
            layout.bubble_size = QSizeF(bubble_width, bubble_height)
            height = bubble_height
            if layout.name_text:
                height += time_metrics.height() + 2
        height += self.run_gap if run_start else self.inner_gap
        if day_text:
            height += self.divider_height
        layout.size = QSize(width, int(height + 0.999))
        self._cache[key] = layout
        return layout

    def sizeHint(self, option, index):
        width = option.rect.width() if option.rect.width() > 0 else self.parent().viewport().width()
        return self.layout(index, width, option.font).size

    # Geometry

    def bubble_rect(self, layout, item, rect):
        top = rect.top() + (self.run_gap if layout.run_start else self.inner_gap) + (self.divider_height if layout.day_text else 0)
        if layout.kind == 'note':
            width = layout.bubble_size.width()
            return QRectF(rect.left() + (rect.width() - width) / 2, top + self.padding_v, width, layout.bubble_size.height())
        if layout.name_text:
            top += QFontMetricsF(self._small_font(self.parent().font())).height() + 2
        width = layout.bubble_size.width()
        if item.outgoing:
            left = rect.right() - self.margin - width
        else:
            left = rect.left() + self.margin + self.avatar_size + self.avatar_gap
        return QRectF(left, top, width, layout.bubble_size.height())

    def text_origin(self, layout, bubble):
        if layout.kind == 'note':
            return bubble.topLeft()
        return QPointF(bubble.left() + self.padding_h, bubble.top() + self.padding_v)

    def anchor_at(self, index, rect, position):
        """The link under a point of the view (in viewport coordinates), or ''."""
        item = self._item(index)
        if item is None:
            return ''
        layout = self.layout(index, rect.width(), self.parent().font())
        if layout.kind != 'text':
            return ''
        origin = self.text_origin(layout, self.bubble_rect(layout, item, QRectF(rect)))
        return layout.document.documentLayout().anchorAt(QPointF(position) - origin) or ''

    # Painting

    def _colours(self, palette, outgoing):
        dark = is_dark_theme()
        if outgoing:
            fill = QColor('#2f5b85') if dark else QColor('#d7ebff')
        else:
            fill = QColor('#3b3d40') if dark else QColor('#eef0f2')
        return fill, palette.color(QPalette.ColorRole.Text)

    @staticmethod
    def _text_palette(palette, colour):
        text_palette = QPalette(palette)
        text_palette.setColor(QPalette.ColorRole.Text, colour)
        text_palette.setColor(QPalette.ColorRole.Link, QColor('#9ccfff') if is_dark_theme() else palette.color(QPalette.ColorRole.Link))
        return text_palette

    def paint(self, painter, option, index):
        item = self._item(index)
        if item is None:
            return
        rect = QRectF(option.rect)
        layout = self.layout(index, option.rect.width(), option.font)
        palette = option.palette
        secondary = secondary_text_color(palette)
        small = self._small_font(option.font)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        bubble = self.bubble_rect(layout, item, rect)
        if layout.day_text:
            self._paint_divider(painter, rect, layout.day_text, small, secondary)
        context = QAbstractTextDocumentLayout.PaintContext()
        if layout.kind == 'note':
            context.palette = self._text_palette(palette, secondary)
            painter.translate(self.text_origin(layout, bubble))
            layout.document.documentLayout().draw(painter, context)
            painter.restore()
            return

        if layout.name_text:
            painter.setFont(small)
            painter.setPen(secondary)
            name_rect = QRectF(bubble.left() + self.padding_h, bubble.top() - QFontMetricsF(small).height() - 2, max(bubble.width(), 240), QFontMetricsF(small).height())
            painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, layout.name_text)
        if layout.run_start and not item.outgoing and self.peer_avatar is not None:
            avatar = QRectF(rect.left() + self.margin, bubble.top(), self.avatar_size, self.avatar_size)
            self.peer_avatar(painter, avatar)

        fill, text_colour = self._colours(palette, item.outgoing)
        if layout.mark_kind == 'failed':
            fill = QColor('#6e2b2b') if is_dark_theme() else QColor('#ffd9d6')
        path = QPainterPath()
        path.addRoundedRect(bubble, self.radius, self.radius)
        painter.fillPath(path, fill)

        context.palette = self._text_palette(palette, text_colour if layout.kind == 'text' else secondary)
        painter.save()
        origin = self.text_origin(layout, bubble)
        painter.translate(origin)
        painter.setClipRect(QRectF(0, 0, layout.text_size.width() + 1, layout.text_size.height() + 1))
        layout.document.documentLayout().draw(painter, context)
        painter.restore()

        painter.setFont(small)
        painter.setPen(secondary)
        time_rect = QRectF(bubble.left() + self.padding_h, bubble.bottom() - self.padding_v - QFontMetricsF(small).height(),
                           bubble.width() - 2 * self.padding_h, QFontMetricsF(small).height())
        if layout.mark:
            mark_colour = {'displayed': QColor('#58a6ff') if is_dark_theme() else QColor('#1a73e8'),
                           'failed': QColor('#ff7b72') if is_dark_theme() else QColor('#c62828')}.get(layout.mark_kind, secondary)
            painter.setPen(mark_colour)
            painter.drawText(time_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, layout.mark)
            time_rect.setRight(time_rect.right() - QFontMetricsF(small).horizontalAdvance(layout.mark + '  '))
            painter.setPen(secondary)
        painter.drawText(time_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, layout.time_text)
        painter.restore()

    def _paint_divider(self, painter, rect, text, font, colour):
        """The day: its name in a rounded pill across a thin line, at the top of the row."""
        metrics = QFontMetricsF(font)
        middle = rect.top() + self.divider_height / 2 + 4
        width = metrics.horizontalAdvance(text) + 20
        height = metrics.height() + 6
        pill = QRectF(rect.left() + (rect.width() - width) / 2, middle - height / 2, width, height)
        line_colour = QColor(colour)
        line_colour.setAlpha(70)
        painter.setPen(line_colour)
        painter.drawLine(QPointF(rect.left() + self.margin, middle), QPointF(pill.left() - 6, middle))
        painter.drawLine(QPointF(pill.right() + 6, middle), QPointF(rect.right() - self.margin, middle))
        painter.setPen(colour)
        painter.setFont(font)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)


def _(text):
    from blink.util import translate
    return translate('message_pane', text)
