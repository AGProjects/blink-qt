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
A reply starts with a quote of what it answers (who, and the first line);
clicking the quote goes to that message. A message being pointed out is
flashed (flash()). Under the mouse a bubble shows its actions button (three dots, beside it on
the side facing the middle), which opens the same menu as a right click.
A picture is drawn inline once it is here (MediaCache decodes it off the GUI
thread): at most 320 pixels high, 640 for a large source, at least 120 wide,
with the time over its corner and the caption under it; until then a box of the
same kind says what it is and how far the download got. Any other file is its
icon, its name and "type · size" (with how far the download got, or in red why
it failed); a PDF that is here shows its first page, with "PDF · N pages ·
size" over it. A call is an arrow for its direction (red for one that wants
attention: missed, rejected, failed), what happened ("Missed video call") and
"duration — reason"; a click opens the call's details. Audio that is here (with
QtMultimedia) is a player: play/pause, 48 bars of waveform (the sender's peaks,
else measured from the file) that fill as it plays and seek on a click or a
drag, the position and the length, and a title (a recording's, else the name).
Layouts are cached per message, width and font.
"""

import os

from datetime import date, timedelta

from PyQt6.QtCore import Qt, QLocale, QPointF, QRectF, QSize, QSizeF
from PyQt6.QtGui import QAbstractTextDocumentLayout, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPalette, QTextCharFormat, QTextCursor, QTextDocument, QTextOption
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate

from blink.messagepane.format import bubble_kind, day_label, delivery_mark, linkify, plain_summary, sanitize_html
from blink.widgets.color import is_dark_theme, secondary_text_color


__all__ = ['BubbleDelegate']


class BubbleLayout(object):
    __slots__ = ('kind', 'run_start', 'day_text', 'document', 'text_size', 'bubble_size', 'size', 'time_text', 'mark', 'mark_kind', 'name_text', 'quote_name', 'quote_text', 'quote_height',
                 'image_path', 'image_size', 'file_name', 'file_meta', 'file_note', 'file_error', 'file_icon',
                 'call_arrow', 'call_title', 'call_detail', 'call_attention', 'audio_title')


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
        self.progress_of = None         # callable(message id) -> download fraction or None
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
        search_text = getattr(index.model(), 'search_text', '')
        reply = item.reply
        progress = self.progress_of(item.id) if self.progress_of is not None else None
        progress = None if progress is None else int(progress * 100)
        image_path = self.file_path(item) if item.category in ('image', 'other', 'audio', 'video') else None
        key = (image_path, item.caption, progress, item.id, item.state, item.content_type, len(item.content or ''), width, font.key(), run_start, day_text, search_text,
               (reply['id'], reply['text']) if reply else None)
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
        layout.quote_name = layout.quote_text = ''
        layout.quote_height = 0
        layout.image_path = layout.image_size = None
        if item.category == 'call':
            layout.kind = kind = 'call'
        elif item.category == 'image':
            layout.kind = kind = 'image'
        elif item.category in ('other', 'audio', 'video'):
            from blink.messagepane.media import pdf_available
            from blink.messagepane.audio import audio_available
            # video is a file here until it has a player of its own
            if item.category == 'audio' and image_path and audio_available():
                layout.kind = kind = 'audio'
            else:
                layout.kind = kind = 'pdf' if image_path and image_path.lower().endswith('.pdf') and pdf_available() else 'file'
        layout.name_text = (item.display_name or '') if run_start and not item.outgoing and kind != 'note' else ''
        small = self._small_font(font)
        document = QTextDocument()
        document.setDocumentMargin(0)
        if kind == 'image':
            return self._image_layout(key, layout, item, image_path, width, font, small, progress)
        if kind == 'pdf':
            layout = self._image_layout(key, layout, item, image_path, width, font, small, progress, pdf=True)
            if layout.image_path:
                return layout
            layout.kind = kind = 'file'         # cannot be read: a plain file
        if kind == 'file':
            return self._file_layout(key, layout, item, image_path, width, font, small, progress)
        if kind == 'call':
            return self._call_layout(key, layout, item, width, font, small)
        if kind == 'audio':
            return self._audio_layout(key, layout, item, image_path, width, font, small)
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
                summary = plain_summary(item)
                if item.category in self.summary_icons:
                    summary = '\u2003 ' + summary      # room for the icon drawn in front
                if progress is not None:
                    summary += '  ' + (translate('message_pane', 'downloading %d%%') % progress if progress < 100 else translate('message_pane', 'downloaded'))
                document.setPlainText(summary)
            if search_text and kind == 'text':
                self._highlight(document, search_text)
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
            layout.quote_name = layout.quote_text = ''
            layout.quote_height = 0
            quote_width = 0
            if reply:
                limit = self._bubble_width_limit(width) - 2 * self.padding_h
                layout.quote_name = translate('message_pane', 'You') if reply['outgoing'] else (reply['name'] or translate('message_pane', 'Them'))
                layout.quote_text = time_metrics.elidedText(' '.join(str(reply['text']).split()), Qt.TextElideMode.ElideRight, limit - 14)
                quote_width = min(limit, max(time_metrics.horizontalAdvance(layout.quote_text), time_metrics.horizontalAdvance(layout.quote_name)) + 14)
                layout.quote_height = 2 * time_metrics.height() + 8 + 4      # two lines, padding, gap below
            bubble_width = max(text_size.width(), time_width, quote_width) + 2 * self.padding_h
            bubble_height = text_size.height() + time_metrics.height() + 2 * self.padding_v + layout.quote_height
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

    @staticmethod
    def _highlight(document, text):
        """Mark every occurrence of a searched text (case-insensitive)."""
        highlight = QTextCharFormat()
        highlight.setBackground(QColor('#8a6d00') if is_dark_theme() else QColor('#ffe08a'))
        cursor = document.find(text, 0)
        while not cursor.isNull():
            cursor.mergeCharFormat(highlight)
            cursor = document.find(text, cursor)

    # Pictures

    image_padding = 4
    image_max_height = 320
    image_large_max_height = 640
    image_large_source = 1600       # pixels on the long side
    image_min_width = 120
    image_placeholder = (220, 150)

    _paths = {}         # message id: local path or None

    def file_path(self, item):
        """Where the message's file is here, remembered until forget() (a download changed it)."""
        if item.upload is not None:
            from blink.messagepane.files import local_file
            return local_file(item)         # being sent: where it is sent from
        try:
            return self._paths[item.id]
        except KeyError:
            from blink.messagepane.files import local_file
            path = self._paths[item.id] = local_file(item)
            return path

    def forget(self, message_id):
        self._paths.pop(message_id, None)

    pdf_width = 260
    pdf_max_height = 360

    def _image_layout(self, key, layout, item, path, width, font, small, progress, pdf=False):
        from blink.messagepane.media import MediaCache
        limit = self._bubble_width_limit(width) - 2 * self.image_padding
        if pdf:
            limit = min(limit, self.pdf_width)
        natural = MediaCache.instance().natural_size(path) if path else None
        if pdf and not (natural is not None and natural.isValid() and natural.width() > 0):
            return layout
        if natural is not None and natural.isValid() and natural.width() > 0 and natural.height() > 0:
            if pdf:
                natural = natural.scaled(limit, 100000, Qt.AspectRatioMode.KeepAspectRatio)    # pages are drawn to fit, never at their size in points
            max_height = self.pdf_max_height if pdf else self.image_large_max_height if max(natural.width(), natural.height()) >= self.image_large_source else self.image_max_height
            scale = min(1.0, limit / natural.width(), max_height / natural.height())
            box = (max(1, round(natural.width() * scale)), max(1, round(natural.height() * scale)))
            layout.image_path = path
        else:
            box = (min(self.image_placeholder[0], limit), self.image_placeholder[1])
        layout.image_size = box
        bubble_width = max(box[0], self.image_min_width) + 2 * self.image_padding
        document = QTextDocument()
        document.setDocumentMargin(0)
        caption_height = 0
        text = item.caption
        if item.upload is not None and item.upload['state'] != 'uploaded':
            text = upload_note(item.upload)
        if pdf:
            from blink.messagepane.files import file_info
            info = file_info(item) or {}
            text = info.get('name') or os.path.basename(path)
        if not layout.image_path:
            # what the box stands for until the picture is here
            from blink.messagepane.format import file_name
            text = (file_name(item.content) or translate('message_pane', 'Picture')) + ('\n' + (translate('message_pane', 'downloading %d%%') % progress) if progress is not None and progress < 100 else '')
        if text:
            document.setDefaultFont(font if layout.image_path else small)
            option = QTextOption(Qt.AlignmentFlag.AlignLeft if layout.image_path else Qt.AlignmentFlag.AlignHCenter)
            option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            document.setDefaultTextOption(option)
            document.setPlainText(text)
            document.setTextWidth(bubble_width - 2 * self.padding_h)
            if layout.image_path:
                caption_height = document.size().height() + self.padding_v + 2
        layout.document = document
        layout.text_size = QSizeF(document.size().width(), document.size().height())
        layout.bubble_size = QSizeF(bubble_width, box[1] + 2 * self.image_padding + caption_height)
        height = layout.bubble_size.height()
        if layout.name_text:
            height += QFontMetricsF(small).height() + 2
        height += self.run_gap if layout.run_start else self.inner_gap
        if layout.day_text:
            height += self.divider_height
        layout.size = QSize(width, int(height + 0.999))
        self._cache[key] = layout
        return layout

    # Files

    file_icon_size = 32
    file_min_width = 200

    _mime_icons = {}

    def _mime_icon(self, name):
        from PyQt6.QtCore import QMimeDatabase
        from PyQt6.QtGui import QIcon
        mime = QMimeDatabase().mimeTypeForFile(name, QMimeDatabase.MatchMode.MatchExtension)
        icon = self._mime_icons.get(mime.name())
        if icon is None:
            icon = QIcon.fromTheme(mime.iconName(), QIcon.fromTheme(mime.genericIconName()))
            if icon.isNull():
                from blink.resources import Resources, themed_icon
                icon = themed_icon(Resources.get('icons/paperclip.svg'), '#bdbdbd')
            self._mime_icons[mime.name()] = icon
        return icon, mime

    def _file_layout(self, key, layout, item, path, width, font, small, progress):
        from blink.messagepane.files import failure_reason, file_info
        from blink.messagepane.format import format_size
        info = file_info(item) or {'name': translate('message_pane', 'File'), 'size': None, 'type': ''}
        layout.file_name = info['name']
        layout.file_icon, mime = self._mime_icon(info['name'])
        kind_text = mime.comment() if not mime.isDefault() else (os.path.splitext(info['name'])[1].lstrip('.').upper() or translate('message_pane', 'File'))
        size = info['size'] or (os.path.getsize(path) if path and os.path.exists(path) else None)
        layout.file_meta = ' · '.join(part for part in (kind_text, format_size(size)) if part)
        layout.file_error = False
        layout.file_note = ''
        if item.upload is not None:
            if item.upload['state'] != 'uploaded':
                layout.file_note, layout.file_error = upload_note(item.upload), item.upload['state'] == 'failed'
        elif progress is not None and progress < 100:
            layout.file_note = translate('message_pane', 'downloading %d%%') % progress
        elif not path:
            reason = failure_reason(item)
            if reason:
                layout.file_note, layout.file_error = '⚠ ' + reason, True
        metrics, small_metrics = QFontMetricsF(font), QFontMetricsF(small)
        limit = self._bubble_width_limit(width) - 2 * self.padding_h - self.file_icon_size - 10
        text_width = min(limit, max(metrics.horizontalAdvance(layout.file_name), small_metrics.horizontalAdvance(layout.file_meta),
                                    small_metrics.horizontalAdvance(layout.file_note), small_metrics.horizontalAdvance(layout.time_text + '  ' + (layout.mark or ''))))
        layout.file_name = metrics.elidedText(layout.file_name, Qt.TextElideMode.ElideMiddle, limit)
        layout.text_size = QSizeF(text_width, metrics.height() + small_metrics.height() * (2 if layout.file_note else 1))
        bubble_width = max(self.file_min_width, text_width + self.file_icon_size + 10 + 2 * self.padding_h)
        bubble_height = 2 * self.padding_v + layout.text_size.height() + small_metrics.height()
        layout.bubble_size = QSizeF(bubble_width, bubble_height)
        height = bubble_height
        if layout.name_text:
            height += small_metrics.height() + 2
        height += self.run_gap if layout.run_start else self.inner_gap
        if layout.day_text:
            height += self.divider_height
        layout.size = QSize(width, int(height + 0.999))
        self._cache[key] = layout
        return layout

    # Calls

    def _call_layout(self, key, layout, item, width, font, small):
        from blink.message_envelopes import call_lines, call_needs_attention, call_record, this_device_id
        record = call_record(item.content, item.metadata)
        device_id = this_device_id()
        lines = call_lines(record, device_id) if record else None
        if lines is None:
            title, duration, phrase = plain_summary(item), '', ''
            layout.call_attention = False
        else:
            title, duration, phrase = lines
            layout.call_attention = call_needs_attention(record, device_id)
        layout.mark, layout.mark_kind = '', None        # a call record is not a message that was delivered
        direction = (record or {}).get('direction') or item.direction
        layout.call_arrow = '↗' if direction == 'outgoing' else '↙'
        layout.call_title = title
        layout.call_detail = ' — '.join(part for part in (duration, phrase) if part)
        metrics, small_metrics = QFontMetricsF(font), QFontMetricsF(small)
        arrow_width = metrics.horizontalAdvance(layout.call_arrow + ' ')
        limit = self._bubble_width_limit(width) - 2 * self.padding_h
        text_width = min(limit, max(arrow_width + metrics.horizontalAdvance(title), small_metrics.horizontalAdvance(layout.call_detail),
                                    small_metrics.horizontalAdvance(layout.time_text + '  ' + (layout.mark or ''))))
        lines_height = metrics.height() + (small_metrics.height() if layout.call_detail else 0)
        layout.text_size = QSizeF(text_width, lines_height)
        layout.bubble_size = QSizeF(max(160, text_width + 2 * self.padding_h), 2 * self.padding_v + lines_height + small_metrics.height())
        height = layout.bubble_size.height()
        if layout.name_text:
            height += small_metrics.height() + 2
        height += self.run_gap if layout.run_start else self.inner_gap
        if layout.day_text:
            height += self.divider_height
        layout.size = QSize(width, int(height + 0.999))
        self._cache[key] = layout
        return layout

    # Audio

    audio_width = 300
    audio_button = 34
    audio_wave_height = 30
    audio_bars = 48

    def _audio_layout(self, key, layout, item, path, width, font, small):
        from blink.message_envelopes import recording_title
        from blink.messagepane.files import file_info
        info = file_info(item) or {}
        name = info.get('name') or os.path.basename(path)
        layout.image_path = path
        layout.audio_title = recording_title(name) or (translate('message_pane', 'Voice message') if item.peaks else name)
        small_metrics = QFontMetricsF(small)
        bubble_width = min(self._bubble_width_limit(width), self.audio_width)
        layout.bubble_size = QSizeF(bubble_width, 2 * self.padding_v + small_metrics.height() + max(self.audio_button, self.audio_wave_height) + 4 + small_metrics.height())
        height = layout.bubble_size.height()
        if layout.name_text:
            height += small_metrics.height() + 2
        height += self.run_gap if layout.run_start else self.inner_gap
        if layout.day_text:
            height += self.divider_height
        layout.size = QSize(width, int(height + 0.999))
        self._cache[key] = layout
        return layout

    def _audio_geometry(self, layout, bubble, small):
        """(button rect, waveform rect) inside an audio bubble."""
        top = bubble.top() + self.padding_v + QFontMetricsF(small).height() + 2
        row = max(self.audio_button, self.audio_wave_height)
        button = QRectF(bubble.left() + self.padding_h, top + (row - self.audio_button) / 2, self.audio_button, self.audio_button)
        wave = QRectF(button.right() + 10, top + (row - self.audio_wave_height) / 2, bubble.right() - self.padding_h - button.right() - 10, self.audio_wave_height)
        return button, wave

    def audio_hit(self, index, rect, position):
        """('play', None) on the button, ('seek', fraction) on the waveform, else None."""
        item = self._item(index)
        if item is None or item.category != 'audio':
            return None
        font = self.parent().font()
        layout = self.layout(index, rect.width(), font)
        if layout.kind != 'audio':
            return None
        button, wave = self._audio_geometry(layout, self.bubble_rect(layout, item, QRectF(rect)), self._small_font(font))
        point = QPointF(position)
        if button.adjusted(-4, -4, 4, 4).contains(point):
            return 'play', None
        if wave.adjusted(0, -6, 0, 6).contains(point):
            return 'seek', max(0.0, min(1.0, (point.x() - wave.left()) / max(1.0, wave.width())))
        return None

    def _paint_audio(self, painter, layout, item, bubble, small, secondary, text_colour):
        from blink.messagepane.audio import AudioInfo, AudioPlayer
        from blink.messagepane.format import format_clock, waveform_bars
        player = AudioPlayer.instance()
        fraction = player.fraction(item.id)
        playing = fraction is not None and player.playing
        info = AudioInfo.instance().get(layout.image_path)
        bars = waveform_bars(item.peaks, self.audio_bars) if item.peaks else (info[1] if info else [0.15] * self.audio_bars)
        duration = info[0] if info else None
        accent = QColor('#58a6ff') if is_dark_theme() else QColor('#1a73e8')
        small_metrics = QFontMetricsF(small)
        painter.setFont(small)
        painter.setPen(secondary)
        title_rect = QRectF(bubble.left() + self.padding_h, bubble.top() + self.padding_v, bubble.width() - 2 * self.padding_h, small_metrics.height())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, small_metrics.elidedText(layout.audio_title, Qt.TextElideMode.ElideRight, title_rect.width()))
        button, wave = self._audio_geometry(layout, bubble, small)
        circle = QPainterPath()
        circle.addEllipse(button)
        painter.fillPath(circle, accent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#ffffff'))
        centre, side = button.center(), button.width() * 0.34
        if playing:
            for offset in (-side * 0.45, side * 0.15):
                painter.drawRect(QRectF(centre.x() + offset, centre.y() - side * 0.55, side * 0.3, side * 1.1))
        else:
            from PyQt6.QtGui import QPolygonF
            painter.drawPolygon(QPolygonF([QPointF(centre.x() - side * 0.4, centre.y() - side * 0.6), QPointF(centre.x() - side * 0.4, centre.y() + side * 0.6),
                                           QPointF(centre.x() + side * 0.65, centre.y())]))
        step = wave.width() / len(bars)
        played = fraction or 0.0
        for number, value in enumerate(bars):
            height = max(2.0, value * wave.height())
            bar = QRectF(wave.left() + number * step + step * 0.2, wave.center().y() - height / 2, max(1.0, step * 0.6), height)
            painter.setBrush(accent if (number + 0.5) / len(bars) <= played else secondary)
            painter.drawRoundedRect(bar, bar.width() / 2, bar.width() / 2)
        clock = format_clock(duration) if duration else ''
        if fraction is not None:
            clock = format_clock(player.position()) + (' / ' + format_clock(duration) if duration else '')
        if clock:
            painter.setPen(secondary)
            painter.drawText(QRectF(wave.left(), bubble.bottom() - self.padding_v - small_metrics.height(), wave.width(), small_metrics.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, clock)

    def _paint_call(self, painter, layout, bubble, font, small, secondary, text_colour):
        metrics, small_metrics = QFontMetricsF(font), QFontMetricsF(small)
        attention = QColor('#ff7b72') if is_dark_theme() else QColor('#c62828')
        ok = QColor('#3fb950') if is_dark_theme() else QColor('#2e7d32')
        left, top = bubble.left() + self.padding_h, bubble.top() + self.padding_v
        width = bubble.width() - 2 * self.padding_h
        painter.setFont(font)
        painter.setPen(attention if layout.call_attention else ok)
        painter.drawText(QRectF(left, top, width, metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, layout.call_arrow)
        arrow_width = metrics.horizontalAdvance(layout.call_arrow + ' ')
        painter.setPen(attention if layout.call_attention else text_colour)
        painter.drawText(QRectF(left + arrow_width, top, width - arrow_width, metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(layout.call_title, Qt.TextElideMode.ElideRight, width - arrow_width))
        if layout.call_detail:
            painter.setFont(small)
            painter.setPen(secondary)
            painter.drawText(QRectF(left, top + metrics.height(), width, small_metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             small_metrics.elidedText(layout.call_detail, Qt.TextElideMode.ElideRight, width))

    def _paint_file(self, painter, layout, item, bubble, font, small, secondary, text_colour):
        metrics, small_metrics = QFontMetricsF(font), QFontMetricsF(small)
        icon_box = QRectF(bubble.left() + self.padding_h, bubble.top() + self.padding_v + (metrics.height() + small_metrics.height() - self.file_icon_size) / 2,
                          self.file_icon_size, self.file_icon_size)
        layout.file_icon.paint(painter, icon_box.toRect())
        left = icon_box.right() + 10
        width = bubble.right() - self.padding_h - left
        top = bubble.top() + self.padding_v
        painter.setFont(font)
        painter.setPen(text_colour)
        painter.drawText(QRectF(left, top, width, metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, layout.file_name)
        top += metrics.height()
        painter.setFont(small)
        painter.setPen(secondary)
        painter.drawText(QRectF(left, top, width, small_metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, layout.file_meta)
        if layout.file_note:
            top += small_metrics.height()
            painter.setPen((QColor('#ff7b72') if is_dark_theme() else QColor('#c62828')) if layout.file_error else secondary)
            painter.drawText(QRectF(left, top, width, small_metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, layout.file_note)

    def _paint_image(self, painter, layout, item, bubble, palette, small, secondary):
        from blink.messagepane.media import MediaCache
        box = QRectF(bubble.left() + self.image_padding, bubble.top() + self.image_padding, layout.image_size[0], layout.image_size[1])
        if bubble.width() - 2 * self.image_padding > box.width():
            box.moveLeft(bubble.left() + (bubble.width() - box.width()) / 2)
        clip = QPainterPath()
        clip.addRoundedRect(box, self.radius - 3, self.radius - 3)
        pixmap = None
        if layout.image_path:
            ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
            pixmap = MediaCache.instance().thumbnail(layout.image_path, (box.width() * ratio, box.height() * ratio))
        painter.save()
        painter.setClipPath(clip)
        if pixmap is not None:
            if layout.kind == 'pdf':
                painter.fillRect(box, QColor('#ffffff'))       # pages are drawn without their paper
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(box, pixmap, QRectF(pixmap.rect()))
        else:
            painter.fillRect(box, QColor(0, 0, 0, 30) if not is_dark_theme() else QColor(255, 255, 255, 24))
            if not layout.image_path:
                context = QAbstractTextDocumentLayout.PaintContext()
                context.palette = self._text_palette(palette, secondary)
                painter.translate(box.left() + (box.width() - layout.text_size.width()) / 2, box.top() + (box.height() - layout.text_size.height()) / 2)
                layout.document.documentLayout().draw(painter, context)
        painter.restore()
        # the time (and the delivery state) on a dark pill over the picture's corner
        metrics = QFontMetricsF(small)
        label = layout.time_text + ('  ' + layout.mark if layout.mark else '')
        pill = QRectF(0, 0, metrics.horizontalAdvance(label) + 12, metrics.height() + 4)
        pill.moveBottomRight(box.bottomRight() - QPointF(6, 6))
        pill_path = QPainterPath()
        pill_path.addRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.fillPath(pill_path, QColor(0, 0, 0, 120))
        painter.setFont(small)
        painter.setPen(QColor('#ffffff'))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)
        if layout.kind == 'pdf':
            from blink.messagepane.files import file_info
            from blink.messagepane.format import format_size
            from blink.messagepane.media import page_count
            pages = page_count(layout.image_path)
            info = file_info(item) or {}
            size = info.get('size') or (os.path.getsize(layout.image_path) if os.path.exists(layout.image_path) else None)
            parts = ['PDF', (translate('message_pane', '1 page') if pages == 1 else translate('message_pane', '%d pages') % pages) if pages else '', format_size(size)]
            text = ' · '.join(part for part in parts if part)
            badge = QRectF(0, 0, metrics.horizontalAdvance(text) + 12, metrics.height() + 4)
            badge.moveBottomLeft(box.bottomLeft() + QPointF(6, -6))
            badge_path = QPainterPath()
            badge_path.addRoundedRect(badge, badge.height() / 2, badge.height() / 2)
            painter.fillPath(badge_path, QColor(0, 0, 0, 120))
            painter.setPen(QColor('#ffffff'))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        if layout.image_path and (item.caption or layout.kind == 'pdf'):
            context = QAbstractTextDocumentLayout.PaintContext()
            context.palette = self._text_palette(palette, self._colours(palette, item.outgoing)[1])
            painter.save()
            painter.translate(bubble.left() + self.padding_h, box.bottom() + self.padding_v)
            layout.document.documentLayout().draw(painter, context)
            painter.restore()

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
        return QPointF(bubble.left() + self.padding_h, bubble.top() + self.padding_v + (layout.quote_height or 0))

    def quote_rect(self, layout, bubble):
        return QRectF(bubble.left() + self.padding_h, bubble.top() + self.padding_v, bubble.width() - 2 * self.padding_h, layout.quote_height - 4)

    def quote_at(self, index, rect, position):
        """The reply dict when a point of the view is on a reply's quote, else None."""
        item = self._item(index)
        if item is None or not item.reply:
            return None
        layout = self.layout(index, rect.width(), self.parent().font())
        if not layout.quote_height:
            return None
        bubble = self.bubble_rect(layout, item, QRectF(rect))
        return item.reply if self.quote_rect(layout, bubble).contains(QPointF(position)) else None

    flashed_id = None

    def flash(self, message_id):
        """Point a message out for a moment (a quote was clicked)."""
        from PyQt6.QtCore import QTimer
        self.flashed_id = message_id
        self.parent().viewport().update()
        QTimer.singleShot(1200, self._end_flash)

    def _end_flash(self):
        self.flashed_id = None
        self.parent().viewport().update()

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
        if item.id == self.flashed_id:
            painter.fillPath(path, QColor(255, 200, 0, 110))
        if layout.kind == 'file':
            self._paint_file(painter, layout, item, bubble, option.font, small, secondary, text_colour)
        if layout.kind == 'call':
            self._paint_call(painter, layout, bubble, option.font, small, secondary, text_colour)
        if layout.kind == 'audio':
            self._paint_audio(painter, layout, item, bubble, small, secondary, text_colour)
        if layout.kind in ('image', 'pdf'):
            self._paint_image(painter, layout, item, bubble, palette, small, secondary)
            if option.state & QStyle.StateFlag.State_MouseOver:
                self._paint_actions_button(painter, self.actions_rect(layout, item, bubble), secondary)
            painter.restore()
            return
        if layout.quote_height:
            quote = self.quote_rect(layout, bubble)
            quote_path = QPainterPath()
            quote_path.addRoundedRect(quote, 6, 6)
            painter.fillPath(quote_path, QColor(0, 0, 0, 22) if not is_dark_theme() else QColor(255, 255, 255, 26))
            accent = QColor('#58a6ff') if is_dark_theme() else QColor('#1a73e8')
            painter.fillRect(QRectF(quote.left(), quote.top() + 3, 3, quote.height() - 6), accent)
            metrics = QFontMetricsF(small)
            painter.setFont(small)
            painter.setPen(accent)
            painter.drawText(QRectF(quote.left() + 9, quote.top() + 3, quote.width() - 12, metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, layout.quote_name)
            painter.setPen(secondary)
            painter.drawText(QRectF(quote.left() + 9, quote.top() + 3 + metrics.height(), quote.width() - 12, metrics.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, layout.quote_text)

        if layout.kind not in ('file', 'call', 'audio'):
            context.palette = self._text_palette(palette, text_colour if layout.kind == 'text' else secondary)
            painter.save()
            origin = self.text_origin(layout, bubble)
            painter.translate(origin)
            painter.setClipRect(QRectF(0, 0, layout.text_size.width() + 1, layout.text_size.height() + 1))
            layout.document.documentLayout().draw(painter, context)
            painter.restore()
        if layout.kind == 'summary' and item.category in self.summary_icons:
            line = QFontMetricsF(option.font).height()
            box = QRectF(origin.x(), origin.y() + line * 0.025, line * 0.95, line * 0.95)
            self._summary_icon(item.category).paint(painter, box.toRect())

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
        if option.state & QStyle.StateFlag.State_MouseOver:
            self._paint_actions_button(painter, self.actions_rect(layout, item, bubble), secondary)
        painter.restore()

    # drawn in front of a summary line, for what fonts often lack a glyph for: (file, recoloured in a dark theme)
    summary_icons = {'other': ('icons/paperclip.svg', True), 'location': ('icons/location-pin.svg', False)}
    _icons = {}

    def _summary_icon(self, category):
        dark = is_dark_theme()
        icon = self._icons.get((category, dark))
        if icon is None:
            from PyQt6.QtGui import QIcon
            from blink.resources import Resources, themed_icon
            filename, themed = self.summary_icons[category]
            path = Resources.get(filename)
            icon = self._icons[(category, dark)] = themed_icon(path, '#bdbdbd') if themed else QIcon(path)
        return icon

    actions_size = 22

    def actions_rect(self, layout, item, bubble):
        """Where the actions button of a bubble is: beside it, towards the middle, level with its top."""
        size = self.actions_size
        left = bubble.left() - 6 - size if item.outgoing else bubble.right() + 6
        return QRectF(left, bubble.top() + 2, size, size)

    def actions_at(self, index, rect, position):
        """Whether a point of the view (viewport coordinates) is on the actions button of this row."""
        item = self._item(index)
        if item is None:
            return False
        layout = self.layout(index, rect.width(), self.parent().font())
        if layout.kind == 'note':
            return False
        return self.actions_rect(layout, item, self.bubble_rect(layout, item, QRectF(rect))).contains(QPointF(position))

    def _paint_actions_button(self, painter, rect, colour):
        painter.save()
        background = QColor(0, 0, 0, 40) if not is_dark_theme() else QColor(255, 255, 255, 40)
        path = QPainterPath()
        path.addEllipse(rect)
        painter.fillPath(path, background)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        radius = 1.8
        for offset in (-5, 0, 5):
            painter.drawEllipse(QPointF(rect.center().x() + offset, rect.center().y()), radius, radius)
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


def upload_note(upload):
    """What a file being sent says under its name: uploading, or why it failed."""
    if upload['state'] == 'failed':
        return '⚠ ' + translate('message_pane', 'Not sent: %s (click to retry)') % (upload['reason'] or translate('message_pane', 'failed'))
    return translate('message_pane', 'Uploading…')


def translate(context, text):
    from blink.util import translate as _translate
    return _translate(context, text)


def _(text):
    from blink.util import translate
    return translate('message_pane', text)
