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

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

try:
    from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoFrameFormat, QVideoSink
except ImportError:
    QCamera = None

from blink.logging import ActivityLog
from blink.util import translate


__all__ = ['camera_available', 'take_photo', 'CameraDialog']


jpeg_quality = 92


_logged_cameras = None


def _bus_name(name):
    """A name made of the device's bus path (the ACPI path of its USB port, then vendor:product), not a model name."""
    return name.startswith('\\') or '.RHUB.' in name


def _largest(device):
    return max((video_format.resolution().width() * video_format.resolution().height() for video_format in device.videoFormats()), default=0)


def _device_id(device):
    return bytes(device.id()).decode(errors='replace')


_alternates = {}        # device id: the devices of the same camera, to try in this order
_labels = {}            # device id: the name to show


def camera_label(device):
    return _labels.get(_device_id(device), device.description())


def camera_alternates(device):
    return _alternates.get(_device_id(device), [device])


def usable_cameras():
    """The cameras one can take a picture with, each once (a device standing for it).

    On Linux one webcam is often listed several times: one V4L2 node per stream
    (a metadata node with no picture formats, an infrared one giving grey
    pictures for face login) and, with PipeWire, each again under a name made of
    its bus path (\\_SB_.PCI0...RHUB...-vendor:product). Devices with no picture
    formats and grey only ones are left out; the rest is grouped into cameras by
    model name, bus path names joining the model when there is only one. A
    camera is shown under its model name and stands for all its devices: those
    reached through PipeWire first (it holds the camera, so the V4L2 nodes are
    busy while it runs), then the system's default, the others tried in turn when one gives no picture
    (CameraDialog). (Two identical webcams plugged in at once would show as one.)
    """
    global _logged_cameras
    grey = {QVideoFrameFormat.PixelFormat.Format_Y8, QVideoFrameFormat.PixelFormat.Format_Y16}
    candidates, left_out = [], []
    for device in QMediaDevices.videoInputs():
        formats = device.videoFormats()
        name = device.description()
        if not formats:
            left_out.append(f'{name} [{_device_id(device)}] (no picture formats)')
        elif all(video_format.pixelFormat() in grey for video_format in formats):
            left_out.append(f'{name} [{_device_id(device)}] (grey only, an infrared camera)')
        else:
            candidates.append(device)
    groups = {}         # label: devices
    for device in candidates:
        if not _bus_name(device.description()):
            groups.setdefault(device.description(), []).append(device)
    for device in candidates:
        if _bus_name(device.description()):
            if len(groups) == 1:
                next(iter(groups.values())).append(device)
            elif not groups:
                groups.setdefault(device.description(), []).append(device)
            else:
                left_out.append(f'{device.description()} [{_device_id(device)}] (a bus path name)')
    default_id = _device_id(QMediaDevices.defaultVideoInput())
    cameras = []
    _alternates.clear()
    _labels.clear()
    for label, devices in groups.items():
        # PipeWire's devices (the bus path names) first: PipeWire keeps the camera open (/dev/video0),
        # so opening the V4L2 node directly fails with "Device or resource busy" while it does
        devices.sort(key=lambda device: (not _bus_name(device.description()), _device_id(device) != default_id))
        cameras.append(devices[0])
        for device in devices:
            _alternates[_device_id(device)] = devices[devices.index(device):] + devices[:devices.index(device)]
            _labels[_device_id(device)] = label
    described = [f"{label} ({', '.join(_device_id(device) for device in devices)})" for label, devices in groups.items()]
    if (described, left_out) != _logged_cameras:
        _logged_cameras = (described, left_out)
        ActivityLog().info(f'[camera] Cameras: {"; ".join(described) or "none"}' + (f'; left out: {", ".join(left_out)}' if left_out else ''))
    return cameras


def camera_available():
    """(True, '') or (False, why) for the menu item."""
    if QCamera is None:
        return False, translate('camera', 'Needs QtMultimedia')
    if not usable_cameras():
        return False, translate('camera', 'No camera found')
    return True, ''


def _preferred_camera():
    devices = usable_cameras()
    try:
        from sipsimple.configuration.settings import SIPSimpleSettings
        wanted = str(SIPSimpleSettings().video.device or '')
    except Exception:
        wanted = ''
    for device in devices:
        if wanted and (camera_label(device) == wanted or device.description() == wanted or wanted.startswith(device.description()) or device.description().startswith(wanted)):
            return device
    default = QMediaDevices.defaultVideoInput()
    return next((device for device in devices if device.id() == default.id()), devices[0] if devices else default)


def _best_format(device, compressed=False):
    """The largest picture the camera gives at a usable frame rate, uncompressed (YUYV and the
    like) or compressed (Jpeg)."""
    jpeg = QVideoFrameFormat.PixelFormat.Format_Jpeg
    formats = [video_format for video_format in device.videoFormats()
               if video_format.maxFrameRate() >= 10 and (video_format.pixelFormat() == jpeg) == compressed]
    if not formats:
        return None
    return max(formats, key=lambda video_format: (video_format.resolution().width() * video_format.resolution().height(), video_format.maxFrameRate()))


def _formats_to_try(device):
    """An uncompressed picture first (Jpeg from a webcam can give no frames through Qt's
    GStreamer backend), then Jpeg, then whatever the device starts with."""
    formats = [video_format for video_format in (_best_format(device), _best_format(device, compressed=True)) if video_format is not None]
    return formats + [None]


def _format_name(video_format):
    if video_format is None:
        return 'its own format'
    resolution = video_format.resolution()
    return f"{video_format.pixelFormat().name.replace('Format_', '')} {resolution.width()}x{resolution.height()}"


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
        self.devices = usable_cameras()
        self.device_box = QComboBox(self)
        for device in self.devices:
            self.device_box.addItem(camera_label(device))
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
        # what the camera list shows, and every device the system reports, for when they do not agree
        # the drop-down as the user sees it, and every device the system reports, for when they do not agree
        entries = [f"{number + 1}. {self.device_box.itemText(number)} [{bytes(self.devices[number].id()).decode(errors='replace')}]"
                   + (' (chosen)' if number == index else '') for number in range(self.device_box.count())]
        ActivityLog().info(f"[camera] Camera window opened, drop-down ({'shown' if len(self.devices) > 1 else 'hidden, one camera'}): "
                           + ('; '.join(entries) or 'empty'))
        for device in QMediaDevices.videoInputs():
            formats = device.videoFormats()
            kinds = sorted({video_format.pixelFormat().name.replace('Format_', '') for video_format in formats})
            biggest = max((video_format.resolution() for video_format in formats), key=lambda size: size.width() * size.height(), default=None)
            ActivityLog().info(f"[camera]   system device {device.description()} [{bytes(device.id()).decode(errors='replace')}]: {len(formats)} formats"
                               + (f" ({', '.join(kinds)}, up to {biggest.width()}x{biggest.height()})" if biggest is not None else '')
                               + (' (default)' if device.isDefault() else ''))
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

    no_picture_timeout = 3000       # ms: then the camera's next device is tried

    def _start(self, device):
        # the devices of this camera in turn, until one gives a picture
        self._tries = [(alternate, video_format) for alternate in camera_alternates(device) for video_format in _formats_to_try(alternate)]
        self._open(0)

    release_delay = 500     # ms between closing one device and opening the next: they are one camera (/dev/video0)

    def _open(self, number):
        self._try = number
        self.frame = None
        if self.camera is not None:
            # the device just closed holds the camera until it is really let go: opening another
            # device of it at once fails with "Device or resource busy"
            self._stop()
            QTimer.singleShot(self.release_delay, lambda: self._open_device(number))
        else:
            self._open_device(number)

    def _open_device(self, number):
        if number != self._try or self.camera is not None:
            return          # another device was chosen meanwhile
        device, video_format = self._tries[number]
        self.camera = QCamera(device, self)
        if video_format is not None:
            self.camera.setCameraFormat(video_format)
        self.camera.errorOccurred.connect(self._SH_Error)
        self.session.setCamera(self.camera)
        self.camera.start()
        ActivityLog().info(f'[camera] Started {camera_label(device)} with device {device.description()} [{_device_id(device)}], {_format_name(video_format)}')
        self._show_buttons()
        camera = self.camera
        QTimer.singleShot(self.no_picture_timeout, lambda: self._check_picture(camera))

    def _check_picture(self, camera):
        if camera is not self.camera or self.frame is not None or self.still is not None:
            return
        device, video_format = self._tries[self._try]
        if self._try + 1 < len(self._tries):
            following, following_format = self._tries[self._try + 1]
            ActivityLog().info(f'[camera] No picture from device {device.description()} [{_device_id(device)}], {_format_name(video_format)}, in {self.no_picture_timeout // 1000}s; '
                               f'trying {following.description()} [{_device_id(following)}], {_format_name(following_format)}')
            self._open(self._try + 1)
        else:
            ActivityLog().info(f'[camera] No picture from device {device.description()} [{_device_id(device)}] in {self.no_picture_timeout // 1000}s, the last one')
            self._not_working()

    def _stop(self):
        if self.camera is not None:
            camera, self.camera = self.camera, None
            camera.stop()
            self.session.setCamera(None)
            camera.setParent(None)
            del camera              # released now, not at the next turn of the event loop

    def _SH_DeviceChosen(self, index):
        if 0 <= index < len(self.devices):
            self.still = None
            self._start(self.devices[index])

    def _SH_Error(self, error, text):
        device = self._tries[self._try][0]
        ActivityLog().warning(f'[camera] Device {device.description()} [{_device_id(device)}] failed: {text or error}')
        self._last_error = text or str(error)
        self.frame = None
        if self._try + 1 < len(self._tries):
            self._open(self._try + 1)       # the camera's next device
        else:
            self._not_working()

    _last_error = ''

    def _not_working(self):
        """Every device of the camera failed or gave no picture: say so, in the view."""
        self._stop()
        label = camera_label(self._tries[self._try][0]) if self._tries else ''
        ActivityLog().warning(f'[camera] {label} is not working' + (f': {self._last_error}' if self._last_error else ': it gives no picture'))
        reason = self._last_error or translate('camera', 'it gives no picture')
        self.preview.setText(translate('camera', 'The camera %s is not working: %s.\n\nIt may be in use by another application or a video call, '
                                                 'or need to be unplugged (or the computer restarted).') % (label, reason))
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
            device = self._tries[self._try][0]
            ActivityLog().info(f'[camera] First picture from device {device.description()} [{_device_id(device)}]: {image.width()}x{image.height()}')
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
