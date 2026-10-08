"""My Picture: the user's own picture, from the camera or a file, cropped to a square.

Clicking one's own picture in the main window opens the camera, as on macOS:
Take Photo, Retake, Use Photo; Choose File... takes a picture from disk
instead, and Remove Picture goes back to the default one. Whatever the
picture, it then goes through Crop: a square drawn as the round avatar it
becomes, the picture dragged under it and zoomed (slider or wheel) until the
face sits in it. The result is a square PNG of avatar_size pixels.
Without a camera (or QtMultimedia) the camera part is left out.
"""

import os
import tempfile
import uuid

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QSlider, QVBoxLayout, QWidget

from blink.logging import ActivityLog
from blink.util import translate


__all__ = ['choose_picture', 'CropDialog']


avatar_size = 256           # the stored picture (IconManager keeps at most 256 px)
image_filter = 'Images (*.png *.jpg *.jpeg *.tiff *.tif *.bmp *.gif *.webp *.svg)'


class CropView(QWidget):
    """The picture under a round window: dragged to move it, the wheel to zoom it."""

    side = 320              # the crop square, in pixels on screen
    margin = 24
    max_zoom = 5.0

    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.image = image
        self.zoom = 1.0
        self.center = QPointF(image.width() / 2, image.height() / 2)    # the picture point under the middle of the square
        self._drag = None
        self.slider = None
        self.setFixedSize(self.side + 2 * self.margin, self.side + 2 * self.margin)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    @property
    def scale(self):
        return self.side / min(self.image.width(), self.image.height()) * self.zoom

    def _clamp(self):
        # the square never leaves the picture
        half = self.side / 2 / self.scale
        x = min(max(self.center.x(), half), self.image.width() - half)
        y = min(max(self.center.y(), half), self.image.height() - half)
        self.center = QPointF(x, y)

    def set_zoom(self, zoom):
        self.zoom = min(max(zoom, 1.0), self.max_zoom)
        self._clamp()
        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(round(self.zoom * 100))
            self.slider.blockSignals(False)
        self.update()

    def _draw_picture(self, painter, origin, side):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.translate(origin.x() + side / 2, origin.y() + side / 2)
        painter.scale(self.scale * side / self.side, self.scale * side / self.side)
        painter.translate(-self.center)
        painter.drawImage(QPointF(0, 0), self.image)
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor('#202020'))
        square = QRectF(self.margin, self.margin, self.side, self.side)
        self._draw_picture(painter, square.topLeft(), self.side)
        shade = QPainterPath()
        shade.addRect(QRectF(self.rect()))
        circle = QPainterPath()
        circle.addEllipse(square)
        painter.fillPath(shade.subtracted(circle), QColor(0, 0, 0, 150))
        painter.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(square)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (event.position(), QPointF(self.center))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            start, center = self._drag
            self.center = center - (event.position() - start) / self.scale
            self._clamp()
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        if steps:
            self.set_zoom(self.zoom * (1.1 ** steps))

    def cropped(self, size=avatar_size):
        result = QImage(size, size, QImage.Format.Format_ARGB32)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        self._draw_picture(painter, QPointF(0, 0), size)
        painter.end()
        return result


class CropDialog(QDialog):
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate('avatar', 'Crop My Picture'))
        self.result_image = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)
        hint = QLabel(translate('avatar', 'Drag the picture to place it, zoom with the slider or the wheel.'), self)
        layout.addWidget(hint)
        self.view = CropView(image, self)
        layout.addWidget(self.view, 0, Qt.AlignmentFlag.AlignHCenter)
        slider = QSlider(Qt.Orientation.Horizontal, self)
        slider.setRange(100, round(CropView.max_zoom * 100))
        slider.setValue(100)
        slider.valueChanged.connect(lambda value: self.view.set_zoom(value / 100))
        self.view.slider = slider
        layout.addWidget(slider)
        buttons = QHBoxLayout()
        cancel = QPushButton(translate('avatar', 'Cancel'), self)
        use = QPushButton(translate('avatar', 'Set Picture'), self)
        use.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(use)
        layout.addLayout(buttons)
        cancel.clicked.connect(self.reject)
        use.clicked.connect(self._use)

    def _use(self):
        self.result_image = self.view.cropped()
        self.accept()


def _camera_dialog_class():
    from blink.messagepane.camera import CameraDialog, camera_available
    if not camera_available()[0]:
        return None

    class PictureCameraDialog(CameraDialog):
        """The camera dialog of the message pane, for one's own picture: with Choose File... and Remove Picture."""

        def __init__(self, parent, directory):
            super().__init__(parent)
            self.setWindowTitle(translate('avatar', 'My Picture'))
            self.directory = directory
            self.choice = None          # ('image', QImage), ('file', path) or ('remove', None)
            buttons = self.layout().itemAt(self.layout().count() - 1).layout()
            self.file_button = QPushButton(translate('avatar', 'Choose File...'), self)
            self.remove_button = QPushButton(translate('avatar', 'Remove Picture'), self)
            buttons.insertWidget(1, self.file_button)
            buttons.insertWidget(2, self.remove_button)
            self.file_button.clicked.connect(self._choose_file)
            self.remove_button.clicked.connect(self._remove)

        def _choose_file(self):
            path = QFileDialog.getOpenFileName(self, translate('avatar', 'Choose a Picture'), self.directory, image_filter)[0]
            if path:
                self.choice = ('file', path)
                self.accept()

        def _remove(self):
            self.choice = ('remove', None)
            self.accept()

        def _use(self):
            if self.still is None:
                return
            self.choice = ('image', self.still.copy())
            self.accept()

    return PictureCameraDialog


def choose_picture(parent, directory=''):
    """('set', path of a square PNG), ('remove', None) or None (cancelled); and the directory
    a file was chosen from, for the next time."""
    dialog_class = _camera_dialog_class()
    if dialog_class is not None:
        dialog = dialog_class(parent, directory)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.choice is None:
            return None, directory
        kind, value = dialog.choice
    else:
        # no camera: a file, or back to the default picture
        from PyQt6.QtGui import QCursor
        menu = QMenu(parent)
        file_action = menu.addAction(translate('avatar', 'Choose File...'))
        remove_action = menu.addAction(translate('avatar', 'Remove Picture'))
        chosen = menu.exec(QCursor.pos())
        if chosen is remove_action:
            kind, value = 'remove', None
        elif chosen is file_action:
            value = QFileDialog.getOpenFileName(parent, translate('avatar', 'Choose a Picture'), directory, image_filter)[0]
            if not value:
                return None, directory
            kind = 'file'
        else:
            return None, directory
    if kind == 'remove':
        ActivityLog().info('[ui] My picture removed')
        return ('remove', None), directory
    if kind == 'file':
        directory = os.path.dirname(value)
        image = QImage(value)
        if image.isNull():
            ActivityLog().warning(f'[ui] Cannot read the picture {value}')
            return None, directory
        what = os.path.basename(value)
    else:
        image, what = value, 'the camera'
    crop = CropDialog(image, parent)
    if crop.exec() != QDialog.DialogCode.Accepted or crop.result_image is None:
        return None, directory
    path = os.path.join(tempfile.gettempdir(), f'blink-avatar-{uuid.uuid4().hex[:8]}.png')
    if not crop.result_image.save(path, 'PNG'):
        ActivityLog().warning(f'[ui] Cannot write {path}')
        return None, directory
    ActivityLog().info(f'[ui] My picture set from {what} ({image.width()}x{image.height()}, cropped to {avatar_size}x{avatar_size})')
    return ('set', path), directory
