
"""Background blur for our own camera in the video rooms.

One reader of the camera feeds both the room's encoder and our own tile, so what we see of
ourselves is what the others get. A person segmentation model (PP-HumanSeg, from the OpenCV
model zoo, run in numpy by blink.webrtc.numpy_net) finds us; everything else is blurred. The
scaling is done by Qt (QImage), the rest in numpy: nothing else is needed.

Two threads, never pjsip's video thread: one blurs every frame with the latest mask (the newest
frame wins, the ones that come while one is processed are dropped), the other finds the person
in the newest frame as often as it can (the model is most of the cost). When the model cannot be
used (it fails to load or to run) the whole picture is blurred: the option is there so that the
room cannot be seen, and it must not fall back to showing it. Until the first mask is made the
whole picture is blurred too.

Only the WebRTC video rooms go through here: SIP video calls send the camera from the SDK.
"""

import os
import threading

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from blink.logging import ActivityLog
from blink.resources import Resources

try:
    import numpy as np
except ImportError:
    np = None

__all__ = ['BlurredCamera', 'blur_available', 'blur_enabled', 'set_blur_enabled']


MODEL = 'models/pphumanseg.npz'
MODEL_SIZE = 192                    # the model's input, square
MAX_WIDTH, MAX_HEIGHT = 640, 480    # the room sends VGA (videoroom.VIDEO_WIDTH/HEIGHT): no need to work on more
BLUR_SCALE = 8                      # the background is blurred at 1/8 of the size, then scaled back up
BLUR_PASSES = 3                     # box blurs at that size: three are close to a gaussian
BLUR_RADIUS = 2
MASK_SMOOTHING = 0.5                # how much of the previous mask is kept: less flicker at the edges
MASK_LOW, MASK_HIGH = 0.25, 0.75    # the person's probability ramped to 0..1 between these: a firm edge


def blur_available():
    return np is not None and os.path.isfile(Resources.get(MODEL))


def blur_enabled():
    from sipsimple.configuration.settings import SIPSimpleSettings
    return blur_available() and SIPSimpleSettings().video.background_blur


def set_blur_enabled(enabled):
    """From the video room's Camera menu or the Preferences: saved, and the open rooms read the
    camera again, through the blur or not (every room sends the same camera)."""
    from sipsimple.configuration.settings import SIPSimpleSettings
    settings = SIPSimpleSettings()
    if settings.video.background_blur == bool(enabled):
        return
    settings.video.background_blur = bool(enabled)
    settings.save()
    ActivityLog().info(f'[videoroom] Background blur {"on" if enabled else "off"}')
    from blink.webrtc.videoroomwindow import VideoroomWindow
    for session, window in list(VideoroomWindow.windows.items()):
        session.blur_changed()
        window.local_tile.attach_camera()


def _fit(width, height):
    scale = min(1.0, MAX_WIDTH / width, MAX_HEIGHT / height)
    return max(2, int(width * scale) & ~1), max(2, int(height * scale) & ~1)


def _array(image):
    """A QImage (32 bit) as a height x width x 4 uint8 array (a copy)."""
    width, height = image.width(), image.height()
    data = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8)
    return data.reshape((height, image.bytesPerLine()))[:, :width * 4].reshape((height, width, 4)).copy()


def _image(array):
    height, width = array.shape[:2]
    array = np.ascontiguousarray(array)
    return QImage(array.tobytes(), width, height, width * 4, QImage.Format.Format_RGB32).copy()


def _box_blur(array, radius):
    """Separable box blur of a small height x width x 4 array, the edges repeated."""
    result = array.astype(np.float32)
    size = 2 * radius + 1
    for axis in (0, 1):
        padded = np.pad(result, [(radius, radius) if a == axis else (0, 0) for a in range(3)], mode='edge')
        total = np.cumsum(padded, axis=axis, dtype=np.float32)
        total = np.concatenate([np.zeros_like(np.take(total, [0], axis=axis)), total], axis=axis)
        length = array.shape[axis]
        result = (np.take(total, np.arange(size, size + length), axis=axis) - np.take(total, np.arange(length), axis=axis)) / size
    return result


def blurred_background(image):
    width, height = image.width(), image.height()
    small = image.scaled(max(1, width // BLUR_SCALE), max(1, height // BLUR_SCALE), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    array = _array(small)
    for _ in range(BLUR_PASSES):
        array = _box_blur(array, BLUR_RADIUS)
    small = _image(np.clip(array + 0.5, 0, 255).astype(np.uint8))
    return small.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


class Segmenter(object):
    """The person's probability at the model's size (float32, 0 .. 1), smoothed over time."""

    def __init__(self):
        from blink.webrtc.numpy_net import NumpyNet
        self.net = NumpyNet(Resources.get(MODEL))
        self.previous = None

    def probability(self, image):
        small = image.scaled(MODEL_SIZE, MODEL_SIZE, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        bgra = _array(small).astype(np.float32)
        # RGB, normalized to -1..1 (mean 0.5, std 0.5 of 0..1), as the model was trained
        rgb = bgra[:, :, 2::-1].transpose(2, 0, 1) / 127.5 - 1.0
        probability = self.net(rgb[None])[0, 1]     # the model ends in a softmax: background, person
        if self.previous is not None:
            probability = MASK_SMOOTHING * self.previous + (1 - MASK_SMOOTHING) * probability
        self.previous = probability
        return probability


def _mask(probability, width, height):
    """The probability ramped to a firm edge and scaled to the frame: uint8, height x width."""
    ramp = np.clip((probability - MASK_LOW) / (MASK_HIGH - MASK_LOW) * 255 + 0.5, 0, 255).astype(np.uint8)
    data = ramp.tobytes()
    image = QImage(data, ramp.shape[1], ramp.shape[0], ramp.shape[1], QImage.Format.Format_Grayscale8)
    image = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    data = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8)
    return data.reshape((height, image.bytesPerLine()))[:, :width]


def blur_frame(image, probability):
    """A frame (QImage, RGB32, at most VGA) blurred around the person; with no probability, all of it."""
    background = blurred_background(image)
    if probability is None:
        return background
    width, height = image.width(), image.height()
    mask = _mask(probability, width, height).astype(np.uint16)[:, :, None]
    blended = (_array(image).astype(np.uint16) * mask + _array(background).astype(np.uint16) * (255 - mask) + 127) // 255
    return _image(blended.astype(np.uint8))


class BlurredCamera(object):
    """The camera, its background blurred, to any number of listeners (called on the blur thread
    with BGRA bytes, width and height). The camera is read only while there are listeners."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = object.__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._listeners = {}
        self._pending = None            # the newest frame, for the blur thread
        self._to_segment = None         # the newest frame, for the segmentation thread
        self._probability = None        # the latest mask
        self._renderer = None
        self._producer = None
        self._threads = []
        self._segmenter = None
        self._segmenter_failed = False

    # From the GUI thread

    def add_listener(self, owner, callback):
        with self._lock:
            self._listeners[id(owner)] = callback
        self.refresh()

    def remove_listener(self, owner):
        with self._lock:
            self._listeners.pop(id(owner), None)
            empty = not self._listeners
        if empty:
            self._detach()

    def refresh(self):
        """Read the camera Blink has now (it was switched), if there is someone to send it to."""
        from sipsimple.application import SIPApplication
        with self._lock:
            if not self._listeners:
                return
        try:
            producer = SIPApplication.video_device.producer
        except AttributeError:
            producer = None
        if producer is self._producer and self._renderer is not None:
            return
        self._detach()
        if producer is None:
            return
        from sipsimple.core import FrameBufferVideoRenderer
        try:
            self._renderer = FrameBufferVideoRenderer(self._on_frame)
            self._renderer.producer = producer
            self._producer = producer
        except Exception as e:
            ActivityLog().warning(f'[videoroom] Cannot read the camera for the background blur: {e}')
            self._detach()
            return
        self._start_threads()

    def _detach(self):
        renderer, self._renderer, self._producer = self._renderer, None, None
        if renderer is not None:
            try:
                renderer.close()
            except Exception:
                pass
        with self._lock:
            self._pending = self._to_segment = None
            self._probability = None        # another camera, or later: the old mask does not fit
            if self._segmenter is not None:
                self._segmenter.previous = None
            self._wakeup.notify_all()

    def _start_threads(self):
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        names = {thread.name for thread in self._threads}
        for name, target in (('background-blur', self._run_blur), ('background-segmentation', self._run_segmentation)):
            if name not in names:
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self._threads.append(thread)

    # pjsip's video thread: keep the newest frame and return

    def _on_frame(self, frame):
        data = bytes(frame.data)
        with self._lock:
            self._pending = (data, frame.width, frame.height)
            self._wakeup.notify_all()

    # The blur thread

    def _run_blur(self):
        while True:
            with self._lock:
                while self._pending is None and self._listeners:
                    self._wakeup.wait(1.0)
                if not self._listeners:
                    self._wakeup.notify_all()
                    return
                (data, width, height), self._pending = self._pending, None
                listeners = list(self._listeners.values())
                probability = self._probability
            if len(data) < width * height * 4:
                continue        # not BGRA (the screen device is I420): never comes here, the screen is not blurred
            try:
                image = QImage(data, width, height, width * 4, QImage.Format.Format_RGB32)
                size = _fit(width, height)
                image = image.scaled(*size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation) if size != (width, height) else image.copy()
                with self._lock:
                    self._to_segment = image
                    self._wakeup.notify_all()
                frame = blur_frame(image, probability)
            except Exception as e:
                ActivityLog().warning(f'[videoroom] The background blur failed: {e}')
                continue
            frame = frame.convertToFormat(QImage.Format.Format_ARGB32)
            result = (frame.constBits().asstring(frame.sizeInBytes()), frame.width(), frame.height())
            for callback in listeners:
                try:
                    callback(*result)
                except Exception as e:
                    ActivityLog().warning(f'[videoroom] Background blur listener failed: {e}')

    # The segmentation thread

    def _run_segmentation(self):
        while True:
            with self._lock:
                while self._to_segment is None and self._listeners:
                    self._wakeup.wait(1.0)
                if not self._listeners:
                    return
                image, self._to_segment = self._to_segment, None
            if self._segmenter_failed:
                continue
            if self._segmenter is None:
                try:
                    self._segmenter = Segmenter()
                except Exception as e:
                    self._segmenter_failed = True
                    ActivityLog().warning(f'[videoroom] Cannot load the background blur model ({e}): the whole picture is blurred')
                    continue
            try:
                probability = self._segmenter.probability(image)
            except Exception as e:
                self._segmenter, self._segmenter_failed = None, True
                with self._lock:
                    self._probability = None
                ActivityLog().warning(f'[videoroom] The background segmentation failed ({e}): the whole picture is blurred')
                continue
            with self._lock:
                if self._listeners and self._renderer is not None:
                    self._probability = probability
