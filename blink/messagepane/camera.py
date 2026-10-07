"""Take a Photo: a picture from the computer's camera, to send in a conversation.

A live preview with a shutter under it (mirrored, as a camera pointed at
oneself is expected to look; the picture itself is not); Take Photo freezes
it, Retake goes back to the live picture, Use Photo hands the file on, to the
same preview the other attachments go through. The camera is the one chosen
in Blink's video settings when it is there, else the system's default; with
several, a list to switch. The camera is stopped as soon as the window goes.
It can be in use by a video call, in which case the window says so.
"""

import os
import tempfile
import uuid

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

try:
    from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink
except ImportError:
    QCamera = None

from blink.logging import ActivityLog
from blink.util import translate


__all__ = ['camera_available', 'take_photo', 'CameraDialog']


jpeg_quality = 92


def camera_available():
    """(True, '') or (False, why) for the menu item."""
    if QCamera is None:
        return False, translate('camera', 'Needs QtMultimedia')
    if not QMediaDevices.videoInputs():
        return False, translate('camera', 'No camera found')
    return True, ''


def _preferred_camera():
    devices = QMediaDevices.videoInputs()
    try:
        from sipsimple.configuration.settings import SIPSimpleSettings
        wanted = str(SIPSimpleSettings().video.device or '')
    except Exception:
        wanted = ''
    for device in devices:
        if wanted and (device.description() == wanted or wanted.startswith(device.description()) or device.description().startswith(wanted)):
            return device
    return QMediaDevices.defaultVideoInput()


def _best_format(device):
    """The largest picture the camera gives at a usable frame rate."""
    formats = [video_format for video_format in device.videoFormats() if video_format.maxFrameRate() >= 10]
    if not formats:
        return None
    return max(formats, key=lambda video_format: (video_format.resolution().width() * video_format.resolution().height(), video_format.maxFrameRate()))


class CameraDialog(QDialog):
    preview_size = QSize(560, 420)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate('camera', 'Take a Photo'))
        self.path = None
        self.frame = None           # the newest frame (QImage), as the camera gives it
        self.still = None           # the frame taken
        self.camera = None
        self.session = QMediaCaptureSession(self)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._SH_Frame)
        self.session.setVideoSink(self.sink)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)
        self.devices = QMediaDevices.videoInputs()
        self.device_box = QComboBox(self)
        for device in self.devices:
            self.device_box.addItem(device.description())
        self.device_box.setVisible(len(self.devices) > 1)
        self.device_box.currentIndexChanged.connect(self._SH_DeviceChosen)
        layout.addWidget(self.device_box)
        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(self.preview_size)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setStyleSheet('background: black; color: #d0d0d0; border-radius: 6px;')
        self.preview.setWordWrap(True)
        self.preview.setText(translate('camera', 'Starting the camera…'))
        layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton(translate('camera', 'Cancel'), self)
        self.retake_button = QPushButton(translate('camera', 'Retake'), self)
        self.take_button = QPushButton(translate('camera', 'Take Photo'), self)
        self.use_button = QPushButton(translate('camera', 'Use Photo'), self)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.retake_button)
        buttons.addWidget(self.take_button)
        buttons.addWidget(self.use_button)
        layout.addLayout(buttons)
        self.cancel_button.clicked.connect(self.reject)
        self.retake_button.clicked.connect(self._retake)
        self.take_button.clicked.connect(self._take)
        self.use_button.clicked.connect(self._use)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._shutter)
        self._show_buttons()
        preferred = _preferred_camera()
        index = next((number for number, device in enumerate(self.devices) if device.id() == preferred.id()), 0)
        if index != self.device_box.currentIndex():
            self.device_box.setCurrentIndex(index)       # starts it
        elif self.devices:
            self._start(self.devices[index])

    def _show_buttons(self):
        taken = self.still is not None
        self.take_button.setVisible(not taken)
        self.take_button.setEnabled(self.frame is not None)
        self.retake_button.setVisible(taken)
        self.use_button.setVisible(taken)
        (self.use_button if taken else self.take_button).setDefault(True)

    def _start(self, device):
        self._stop()
        self.frame = None
        self.camera = QCamera(device, self)
        video_format = _best_format(device)
        if video_format is not None:
            self.camera.setCameraFormat(video_format)
        self.camera.errorOccurred.connect(self._SH_Error)
        self.session.setCamera(self.camera)
        self.camera.start()
        resolution = video_format.resolution() if video_format is not None else None
        ActivityLog().info(f'[camera] Started {device.description()}' + (f' at {resolution.width()}x{resolution.height()}' if resolution else ''))
        self._show_buttons()

    def _stop(self):
        if self.camera is not None:
            self.camera.stop()
            self.session.setCamera(None)
            self.camera.deleteLater()
            self.camera = None

    def _SH_DeviceChosen(self, index):
        if 0 <= index < len(self.devices):
            self.still = None
            self._start(self.devices[index])

    def _SH_Error(self, error, text):
        ActivityLog().warning(f'[camera] {text or error}')
        self.preview.setText(translate('camera', 'The camera cannot be used: %s\n\nIt may be in use by a video call.') % (text or error))
        self.frame = None
        self._show_buttons()

    def _SH_Frame(self, frame):
        if self.still is not None or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        first = self.frame is None
        self.frame = image
        self._paint(image.mirrored(True, False))     # a mirror, as people expect to see themselves
        if first:
            self._show_buttons()

    def _paint(self, image):
        pixmap = QPixmap.fromImage(image).scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pixmap)

    def _shutter(self):
        if self.still is None:
            self._take()
        else:
            self._use()

    def _take(self):
        if self.frame is None:
            return
        self.still = self.frame.copy()
        self._paint(self.still)           # what will be sent, the way it will be sent
        self._show_buttons()

    def _retake(self):
        self.still = None
        self._show_buttons()

    def _use(self):
        if self.still is None:
            return
        path = os.path.join(tempfile.gettempdir(), f'blink-photo-{uuid.uuid4().hex[:8]}.jpg')
        if not self.still.convertToFormat(QImage.Format.Format_RGB888).save(path, 'JPEG', jpeg_quality):
            ActivityLog().warning(f'[camera] Cannot write {path}')
            return
        ActivityLog().info(f'[camera] Photo taken: {self.still.width()}x{self.still.height()}, {path}')
        self.path = path
        self.accept()

    def done(self, result):
        self._stop()            # no camera left running behind a closed window
        super().done(result)


def take_photo(parent=None):
    """The path of a photo taken now, or None."""
    dialog = CameraDialog(parent)
    dialog.exec()
    return dialog.path
