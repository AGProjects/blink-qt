
"""Background blur for our own camera in the video rooms.

One reader of the camera feeds both the room's encoder and our own tile, so what we see of
ourselves is what the others get. A person segmentation model (PP-HumanSeg, from the OpenCV
model zoo, run with OpenCV's dnn module) finds us in each frame; everything else is blurred.

The work is done on a thread of its own, never on pjsip's video thread: the newest frame wins
and the ones that come while one is processed are dropped. When the model cannot be used (it
fails to load or to run) the whole picture is blurred: the option is there so that the room
cannot be seen, and it must not fall back to showing it.

Only the WebRTC video rooms go through here: SIP video calls send the camera from the SDK.
"""

import os
import threading

from blink.logging import ActivityLog
from blink.resources import Resources

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None

__all__ = ['BlurredCamera', 'blur_available', 'blur_enabled', 'set_blur_enabled']


MODEL = 'models/human_segmentation_pphumanseg_2023mar.onnx'
MODEL_SIZE = 192                    # the model's input, square
MAX_WIDTH, MAX_HEIGHT = 640, 480    # the room sends VGA (videoroom.VIDEO_WIDTH/HEIGHT): no need to work on more
BLUR_SCALE = 8                      # the background is blurred at 1/8 of the size, then scaled back up
BLUR_SIGMA = 3                      # at that size
MASK_SMOOTHING = 0.5                # how much of the previous mask is kept: less flicker at the edges
MASK_LOW, MASK_HIGH = 0.25, 0.75    # the person's probability ramped to 0..1 between these: a firm edge
FEATHER = 7                         # the edge softened over this many pixels (odd)
SEGMENT_EVERY = 2                   # the person is found in one frame of this many


def blur_available():
    return cv2 is not None and os.path.isfile(Resources.get(MODEL))


def blur_enabled():
    from sipsimple.configuration.settings import SIPSimpleSettings
    return blur_available() and SIPSimpleSettings().video.background_blur


def set_blur_enabled(enabled):
    from sipsimple.configuration.settings import SIPSimpleSettings
    settings = SIPSimpleSettings()
    settings.video.background_blur = bool(enabled)
    settings.save()


class Segmenter(object):
    """The person's mask for a frame: float32, 0 (background) .. 1 (person), at the frame's size."""

    def __init__(self):
        self.net = cv2.dnn.readNetFromONNX(Resources.get(MODEL))
        self.previous = None
        self.last = None
        self.frames = 0

    def mask(self, bgr):
        height, width = bgr.shape[:2]
        self.frames += 1
        if self.frames % SEGMENT_EVERY and self.last is not None and self.last.shape == (height, width):
            return self.last        # the model is most of the cost: every other frame reuses the mask
        # RGB, normalized to -1..1 (mean 0.5, std 0.5 of 0..1), as the model was trained
        blob = cv2.dnn.blobFromImage(bgr, scalefactor=1 / 127.5, size=(MODEL_SIZE, MODEL_SIZE), mean=(127.5, 127.5, 127.5), swapRB=True)
        self.net.setInput(blob)
        output = self.net.forward()[0]      # 2 x 192 x 192: background, person
        probability = output[1].astype(np.float32)      # the model ends in a softmax
        if self.previous is not None and self.previous.shape == probability.shape:
            probability = MASK_SMOOTHING * self.previous + (1 - MASK_SMOOTHING) * probability
        self.previous = probability
        mask = np.clip((probability - MASK_LOW) / (MASK_HIGH - MASK_LOW), 0.0, 1.0)
        mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_LINEAR)
        self.last = cv2.GaussianBlur(mask, (FEATHER, FEATHER), 0)
        return self.last


def _fit(width, height):
    scale = min(1.0, MAX_WIDTH / width, MAX_HEIGHT / height)
    return max(2, int(width * scale) & ~1), max(2, int(height * scale) & ~1)


def blur_frame(data, width, height, segmenter):
    """BGRA bytes in, BGRA bytes out (at most VGA); with no segmenter, the whole picture is blurred."""
    frame = np.frombuffer(data, dtype=np.uint8, count=width * height * 4).reshape((height, width, 4))
    size = _fit(width, height)
    if size != (width, height):
        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    width, height = size
    small = cv2.resize(frame, (max(1, width // BLUR_SCALE), max(1, height // BLUR_SCALE)), interpolation=cv2.INTER_AREA)
    background = cv2.resize(cv2.GaussianBlur(small, (0, 0), BLUR_SIGMA), (width, height), interpolation=cv2.INTER_LINEAR)
    if segmenter is None:
        return background.tobytes(), width, height
    mask = segmenter.mask(cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR))
    result = cv2.blendLinear(frame.astype(np.float32), background.astype(np.float32), mask, 1.0 - mask)
    return result.astype(np.uint8).tobytes(), width, height


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
        self._pending = None
        self._renderer = None
        self._producer = None
        self._thread = None
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
        self._start_thread()

    def _detach(self):
        renderer, self._renderer, self._producer = self._renderer, None, None
        if renderer is not None:
            try:
                renderer.close()
            except Exception:
                pass
        with self._lock:
            self._pending = None

    def _start_thread(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name='background-blur', daemon=True)
        self._thread.start()

    # pjsip's video thread: keep the newest frame and return

    def _on_frame(self, frame):
        data = bytes(frame.data)
        with self._lock:
            self._pending = (data, frame.width, frame.height)
            self._wakeup.notify()

    # The blur thread

    def _run(self):
        while True:
            with self._lock:
                while self._pending is None and self._listeners:
                    self._wakeup.wait(1.0)
                if not self._listeners:
                    self._thread = None
                    return
                (data, width, height), self._pending = self._pending, None
                listeners = list(self._listeners.values())
            if len(data) < width * height * 4:
                continue        # not BGRA (the screen device is I420): never comes here, the screen is not blurred
            if self._segmenter is None and not self._segmenter_failed:
                try:
                    self._segmenter = Segmenter()
                except Exception as e:
                    self._segmenter_failed = True
                    ActivityLog().warning(f'[videoroom] Cannot load the background blur model ({e}): the whole picture is blurred')
            try:
                frame = blur_frame(data, width, height, self._segmenter)
            except Exception as e:
                if self._segmenter is not None:
                    ActivityLog().warning(f'[videoroom] The background blur failed ({e}): the whole picture is blurred')
                    self._segmenter, self._segmenter_failed = None, True
                try:
                    frame = blur_frame(data, width, height, None)
                except Exception:
                    continue
            for callback in listeners:
                try:
                    callback(*frame)
                except Exception as e:
                    ActivityLog().warning(f'[videoroom] Background blur listener failed: {e}')
