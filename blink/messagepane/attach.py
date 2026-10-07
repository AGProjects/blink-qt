"""The look before sending: every file on its way out of the message pane goes through here.

Files chosen, dropped, pasted or grabbed as a screenshot are shown first
(AttachmentPreview); only Send sends them. One picture is shown as large as
the window holds it and can be cropped (drag a rectangle; drag inside it to
move it, double-click to start over): only that part is sent, as a new file,
what is on disk is never touched. One picture or movie gets a caption field
(the caption goes as a label companion, as Sylk Mobile sends it). Several files
are a list of name and size.

Pictures are made smaller unless "Send original" is ticked: at most
shrink_long_side pixels on the long side, JPEG at jpeg_quality (PNG when the
picture has transparency), turned upright by its EXIF orientation. A picture
already small enough and not cropped goes as it is, and so does a GIF (it may
move). Movies and other files go as they are. The files made here are kept in
ApplicationData 'outgoing/<id>/' under the original name.
"""

import os
import uuid

from PyQt6.QtCore import Qt, QFileInfo, QPointF, QRect, QRectF, QSize
from PyQt6.QtGui import QColor, QIcon, QImage, QImageReader, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileIconProvider, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QSizePolicy, QVBoxLayout, QWidget)

from blink.logging import ActivityLog
from blink.util import translate


__all__ = ['AttachmentPreview', 'prepare_picture', 'is_picture', 'is_movie']


shrink_long_side = 2048
shrink_min_bytes = 1024 * 1024      # a picture smaller than this and within the size is sent as it is
jpeg_quality = 85
crop_quality = 92                   # a crop of an original keeps nearly all of it

PICTURE_EXTENSIONS = ('png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'tif', 'tiff', 'heic', 'heif')
MOVIE_EXTENSIONS = ('mp4', 'm4v', 'mov', '3gp', 'webm', 'mkv', 'avi')


def _extension(path):
    return os.path.splitext(path)[1].lower().lstrip('.')


def is_picture(path):
    return _extension(path) in PICTURE_EXTENSIONS


def is_movie(path):
    return _extension(path) in MOVIE_EXTENSIONS


def _format_size(size):
    from blink.messagepane.format import format_size
    return format_size(size)


def _outgoing_path(name):
    from blink.resources import ApplicationData
    from application.system import makedirs
    directory = ApplicationData.get(f'outgoing/{uuid.uuid4()}')
    makedirs(directory)
    return os.path.join(directory, name)


def prepare_picture(path, crop=None, original=False):
    """The file to send for a picture: path itself, or a new file cropped and/or made smaller."""
    extension = _extension(path)
    if extension == 'gif' and crop is None:
        return path
    try:
        size = os.path.getsize(path)
    except OSError:
        return path
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    natural = reader.size()
    long_side = max(natural.width(), natural.height()) if natural.isValid() else 0
    if crop is None and (original or (long_side and long_side <= shrink_long_side and size <= shrink_min_bytes)):
        return path
    image = reader.read()
    if image.isNull():
        ActivityLog().warning(f'[transfer] Cannot read the picture {path}: {reader.errorString()}; sent as it is')
        return path
    if crop is not None:
        image = image.copy(crop.intersected(image.rect()))
    if not original and max(image.width(), image.height()) > shrink_long_side:
        image = image.scaled(shrink_long_side, shrink_long_side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    base = os.path.splitext(os.path.basename(path))[0] or 'picture'
    if image.hasAlphaChannel() and extension in ('png', 'gif', 'webp', 'tif', 'tiff'):
        target, kind, quality = _outgoing_path(base + '.png'), 'PNG', -1
    else:
        target, kind, quality = _outgoing_path(base + '.jpg'), 'JPEG', crop_quality if original else jpeg_quality
        image = image.convertToFormat(QImage.Format.Format_RGB888)
    if not image.save(target, kind, quality):
        ActivityLog().warning(f'[transfer] Cannot write {target}; {os.path.basename(path)} sent as it is')
        return path
    what = ' and '.join(part for part in ('cropped' if crop is not None else '', 'made smaller' if not original else '') if part)
    ActivityLog().info(f'[transfer] {os.path.basename(path)} {what}: {natural.width()}x{natural.height()}, {_format_size(size)} -> '
                       f'{image.width()}x{image.height()}, {_format_size(os.path.getsize(target))}')
    return target


class CropView(QWidget):
    """A picture fitted in the widget, with a rectangle to crop it to."""

    handle = 8

    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.image = image
        self.pixmap = QPixmap.fromImage(image)
        self.selection = None       # QRectF in image pixels
        self._press = None          # (widget point, selection when pressed, 'new' or 'move')
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(translate('attach', 'Drag to crop, double-click to send it whole'))

    def sizeHint(self):
        return QSize(440, 320)

    def _frame(self):
        """Where the picture is drawn (QRectF) and its scale."""
        if self.image.isNull():
            return QRectF(), 1.0
        scale = min(self.width() / self.image.width(), self.height() / self.image.height())
        width, height = self.image.width() * scale, self.image.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height), scale

    def _to_image(self, point):
        frame, scale = self._frame()
        x = min(max(point.x(), frame.left()), frame.right())
        y = min(max(point.y(), frame.top()), frame.bottom())
        return QPointF((x - frame.left()) / scale, (y - frame.top()) / scale)

    def _to_widget(self, rect):
        frame, scale = self._frame()
        return QRectF(frame.left() + rect.left() * scale, frame.top() + rect.top() * scale, rect.width() * scale, rect.height() * scale)

    def crop(self):
        """The crop as a QRect in image pixels, or None (the whole picture)."""
        if self.selection is None:
            return None
        rect = self.selection.toAlignedRect().intersected(self.image.rect())
        return rect if rect.width() >= 8 and rect.height() >= 8 and rect != self.image.rect() else None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position()
        inside = self.selection is not None and self._to_widget(self.selection).contains(point)
        self._press = (point, QRectF(self.selection) if self.selection is not None else None, 'move' if inside else 'new')

    def mouseMoveEvent(self, event):
        if self._press is None:
            inside = self.selection is not None and self._to_widget(self.selection).contains(event.position())
            self.setCursor(Qt.CursorShape.SizeAllCursor if inside else Qt.CursorShape.CrossCursor)
            return
        start, original, mode = self._press
        if mode == 'move':
            delta = self._to_image(event.position()) - self._to_image(start)
            moved = original.translated(delta)
            bounds = QRectF(self.image.rect())
            moved.moveLeft(min(max(moved.left(), 0), bounds.width() - moved.width()))
            moved.moveTop(min(max(moved.top(), 0), bounds.height() - moved.height()))
            self.selection = moved
        else:
            self.selection = QRectF(self._to_image(start), self._to_image(event.position())).normalized()
        self.update()

    def mouseReleaseEvent(self, event):
        self._press = None
        if self.selection is not None and (self.selection.width() < 8 or self.selection.height() < 8):
            self.selection = None
        self.update()
        self.cropChanged()

    def mouseDoubleClickEvent(self, event):
        self.selection = None
        self.update()
        self.cropChanged()

    def cropChanged(self):
        """Overridden by the dialog to update what it says about the size."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        frame, scale = self._frame()
        painter.drawPixmap(frame, self.pixmap, QRectF(self.pixmap.rect()))
        if self.selection is None:
            return
        rect = self._to_widget(self.selection)
        outside = QPainterPath()
        outside.addRect(frame)
        inside = QPainterPath()
        inside.addRect(rect)
        painter.fillPath(outside.subtracted(inside), QColor(0, 0, 0, 140))
        painter.setPen(QPen(QColor('#ffffff'), 1.5))
        painter.drawRect(rect)
        painter.setBrush(QColor('#ffffff'))
        for corner in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
            painter.drawEllipse(corner, 3.5, 3.5)


class AttachmentPreview(QDialog):
    """Shows what is about to be sent; plan() is [(path to send, caption or '')] after Send."""

    def __init__(self, paths, peer, parent=None):
        super().__init__(parent)
        self.paths = [path for path in paths if os.path.isfile(path)]
        self._plan = []
        self.crop_view = None
        self.caption_edit = None
        self.original_box = None
        self.setWindowTitle(translate('attach', 'Send to %s') % peer if peer else translate('attach', 'Send Files'))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)
        single = self.paths[0] if len(self.paths) == 1 else None
        if single is not None and is_picture(single):
            reader = QImageReader(single)
            reader.setAutoTransform(True)
            natural = reader.size()
            if natural.isValid() and max(natural.width(), natural.height()) > 1600:
                reader.setScaledSize(natural.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if not image.isNull():
                self.crop_view = CropView(image, self)
                self._display_scale = natural.width() / image.width() if natural.isValid() and image.width() else 1.0
                self._natural = natural
                self.crop_view.cropChanged = self._update_facts
                layout.addWidget(self.crop_view, 1)
        elif single is not None and is_movie(single):
            self.poster = QLabel(self)
            self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.poster.setMinimumSize(440, 248)
            self.poster.setStyleSheet('background: black; border-radius: 6px;')
            layout.addWidget(self.poster, 1)
            self._show_poster()
        if single is not None:
            name = QLabel(os.path.basename(single), self)
            name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            layout.addWidget(name)
            self.facts = QLabel(self)
            self.facts.setStyleSheet('color: palette(mid);')
            layout.addWidget(self.facts)
            self._update_facts()
            if is_picture(single) or is_movie(single):
                self.caption_edit = QLineEdit(self)
                self.caption_edit.setPlaceholderText(translate('attach', 'Add a caption'))
                self.caption_edit.setClearButtonEnabled(True)
                layout.addWidget(self.caption_edit)
        else:
            self.list = QListWidget(self)
            self.list.setIconSize(QSize(40, 40))
            self.list.setMinimumSize(420, min(320, 52 * len(self.paths) + 8))
            icons = QFileIconProvider()
            for path in self.paths:
                row = QListWidgetItem(f'{os.path.basename(path)}\n{_format_size(os.path.getsize(path))}')
                thumbnail = None
                if is_picture(path):
                    reader = QImageReader(path)
                    reader.setAutoTransform(True)
                    if reader.size().isValid():
                        reader.setScaledSize(reader.size().scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio))
                    image = reader.read()
                    thumbnail = QPixmap.fromImage(image) if not image.isNull() else None
                row.setIcon(QIcon(thumbnail) if thumbnail is not None else icons.icon(QFileInfo(path)))
                self.list.addItem(row)
            layout.addWidget(self.list, 1)
        if any(is_picture(path) and _extension(path) != 'gif' for path in self.paths):
            self.original_box = QCheckBox(translate('attach', 'Send original') if single else translate('attach', 'Send pictures as originals'), self)
            self.original_box.setToolTip(translate('attach', 'Unticked, pictures are sent at most %d pixels on the long side') % shrink_long_side)
            layout.addWidget(self.original_box)
        buttons = QDialogButtonBox(self)
        self.send_button = buttons.addButton(translate('attach', 'Send'), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.send_button.setDefault(True)
        buttons.accepted.connect(self._send)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.caption_edit is not None:
            self.caption_edit.setFocus()

    def _show_poster(self):
        from blink.messagepane.video import VideoProbe, video_available
        if not video_available():
            return
        probe = VideoProbe.instance()
        info = probe.info(self.paths[0])
        if info is None:
            if not getattr(self, '_waiting', False):
                self._waiting = True
                probe.probed.connect(self._SH_Probed)
            return
        if info:
            image = probe.poster(self.paths[0])
            if image is not None:
                self.poster.setPixmap(QPixmap.fromImage(image).scaled(self.poster.minimumSize(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        if hasattr(self, 'facts'):
            self._update_facts()

    def _SH_Probed(self, path):
        if self.paths and path == self.paths[0]:
            self._show_poster()

    def _update_facts(self):
        path = self.paths[0]
        parts = []
        if self.crop_view is not None:
            crop = self.crop_view.crop()
            if crop is not None:
                width, height = round(crop.width() * self._display_scale), round(crop.height() * self._display_scale)
                parts.append(translate('attach', '%d × %d of %d × %d') % (width, height, self._natural.width(), self._natural.height()))
            elif self._natural.isValid():
                parts.append(f'{self._natural.width()} × {self._natural.height()}')
        elif is_movie(path):
            from blink.messagepane.video import VideoProbe, video_available
            info = VideoProbe.instance().info(path) if video_available() else None
            if info:
                parts.append(f'{info["size"].width()} × {info["size"].height()}')
                if info['duration']:
                    from blink.messagepane.format import format_clock
                    parts.append(format_clock(info['duration']))
        parts.append(_format_size(os.path.getsize(path)))
        self.facts.setText(' · '.join(part for part in parts if part))

    def _send(self):
        original = self.original_box is not None and self.original_box.isChecked()
        caption = self.caption_edit.text().strip() if self.caption_edit is not None else ''
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plan = []
            for path in self.paths:
                if is_picture(path):
                    crop = None
                    if self.crop_view is not None and self.crop_view.crop() is not None:
                        rect = self.crop_view.crop()
                        scale = self._display_scale
                        crop = QRect(round(rect.x() * scale), round(rect.y() * scale), round(rect.width() * scale), round(rect.height() * scale))
                    path = prepare_picture(path, crop, original)
                plan.append((path, caption))
            self._plan = plan
        finally:
            QApplication.restoreOverrideCursor()
        self.accept()

    def plan(self):
        return self._plan
