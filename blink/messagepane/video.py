"""Video for the transcript: a poster frame and the duration, probed off the GUI thread.

VideoProbe.instance().info(path) is None while the file is being probed
(probed(path) is emitted when it is known), {'size': QSize, 'duration': seconds}
for a movie GStreamer can make a picture of, False for one it cannot (the
bubble is then a plain file). The poster is the frame at one second (or a tenth
in, for a shorter clip), turned the way the camera meant (videoflip auto), at
most 1280 pixels on its long side; MediaCache draws it like a picture
(register_decoder for the video extensions). Results are kept per file, its
size and modification time, for the session. Playback itself is AudioPlayer's
(one clip at a time, app-wide), with its frames painted by the bubble.

Needs python3-gi with GStreamer (gir1.2-gstreamer-1.0) and QtMultimedia; without
them a movie is a plain file.
"""

import os
import threading

from PyQt6.QtCore import QObject, QRunnable, QSize, QThreadPool, Qt, pyqtSignal
from PyQt6.QtGui import QImage

from blink.logging import MessagingTrace as log


__all__ = ['VideoProbe', 'video_available', 'VIDEO_EXTENSIONS']


VIDEO_EXTENSIONS = ('mp4', 'mov', 'm4v', '3gp', 'webm', 'mkv', 'avi')

try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
except (ImportError, ValueError):
    Gst = None


def video_available():
    from blink.messagepane.audio import audio_available
    return Gst is not None and audio_available()


poster_max = 1280
probe_timeout = 10      # seconds per step


def _wait(pipeline):
    """Wait for the pipeline to settle (prerolled); False on an error or a timeout."""
    bus = pipeline.get_bus()
    message = bus.timed_pop_filtered(probe_timeout * Gst.SECOND, Gst.MessageType.ASYNC_DONE | Gst.MessageType.ERROR)
    if message is None:
        return False, 'timed out'
    if message.type == Gst.MessageType.ERROR:
        error, _ = message.parse_error()
        return False, error.message
    return True, None


def probe(path):
    """(info, poster QImage) of a movie, or (False, None). Blocks: run it in a worker."""
    pipeline = Gst.ElementFactory.make('playbin', None)
    sink = Gst.ElementFactory.make('appsink', None)
    if pipeline is None or sink is None:
        return False, None
    sink.set_property('caps', Gst.Caps.from_string('video/x-raw,format=RGBA,pixel-aspect-ratio=1/1'))
    sink.set_property('sync', False)
    pipeline.set_property('video-sink', sink)
    pipeline.set_property('audio-sink', Gst.ElementFactory.make('fakesink', None))
    flip = Gst.ElementFactory.make('videoflip', None)
    if flip is not None:
        flip.set_property('video-direction', 8)        # auto: as the orientation tag says
        pipeline.set_property('video-filter', flip)
    pipeline.set_property('uri', Gst.filename_to_uri(os.path.abspath(path)))
    try:
        pipeline.set_state(Gst.State.PAUSED)
        ok, reason = _wait(pipeline)
        if not ok:
            log.warning(f'Cannot read the movie {path}: {reason}')
            return False, None
        found, duration = pipeline.query_duration(Gst.Format.TIME)
        seconds = duration / Gst.SECOND if found and duration > 0 else None
        at = min(Gst.SECOND, duration // 10) if found and duration > 0 else 0
        if at > 0 and pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, at):
            _wait(pipeline)
        sample = sink.emit('pull-preroll')
        if sample is None:
            log.warning(f'No picture in the movie {path}')
            return False, None
        structure = sample.get_caps().get_structure(0)
        width, height = structure.get_value('width'), structure.get_value('height')
        buffer = sample.get_buffer()
        mapped, data = buffer.map(Gst.MapFlags.READ)
        if not mapped:
            return False, None
        try:
            stride = len(data.data) // height if height else width * 4
            image = QImage(bytes(data.data), width, height, stride, QImage.Format.Format_RGBA8888).copy()
        finally:
            buffer.unmap(data)
        if max(width, height) > poster_max:
            image = image.scaled(poster_max, poster_max, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return {'size': QSize(width, height), 'duration': seconds}, image
    except Exception as e:
        log.warning(f'Cannot read the movie {path}: {e!r}')
        return False, None
    finally:
        pipeline.set_state(Gst.State.NULL)


class _Signals(QObject):
    done = pyqtSignal(object, object)        # stamp, info


class _Job(QRunnable):
    def __init__(self, probe_owner, stamp):
        super().__init__()
        self.owner, self.stamp = probe_owner, stamp

    def run(self):
        info = self.owner._probe(self.stamp)
        self.owner._signals.done.emit(self.stamp, info)


class VideoProbe(QObject):
    probed = pyqtSignal(str)        # a path whose info is known now

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._infos = {}        # stamp: info or False
        self._posters = {}      # stamp: QImage
        self._pending = set()
        self._signals = _Signals()
        self._signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    @staticmethod
    def _stamp(path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return path, int(stat.st_mtime_ns), stat.st_size

    def info(self, path):
        """{'size', 'duration'}, False when it cannot be played, or None while it is probed (GUI thread)."""
        stamp = self._stamp(path)
        if stamp is None:
            return False
        with self._lock:
            if stamp in self._infos:
                return self._infos[stamp]
        if stamp not in self._pending:
            self._pending.add(stamp)
            self._pool.start(_Job(self, stamp))
        return None

    def state(self, path):
        info = self.info(path)
        return 'pending' if info is None else 'ok' if info else 'bad'

    def poster(self, path):
        """The poster QImage, probing the file first when needed. Blocks: for MediaCache's workers."""
        stamp = self._stamp(path)
        if stamp is None:
            return None
        with self._lock:
            if stamp in self._infos:
                return self._posters.get(stamp)
        self._probe(stamp)
        with self._lock:
            return self._posters.get(stamp)

    def _probe(self, stamp):
        with self._lock:
            if stamp in self._infos:
                return self._infos[stamp]
        info, image = probe(stamp[0])
        with self._lock:
            self._infos[stamp] = info
            if image is not None:
                self._posters[stamp] = image
        return info

    def _done(self, stamp, info):
        self._pending.discard(stamp)
        if info:
            log.info(f'Movie {os.path.basename(stamp[0])}: {info["size"].width()}x{info["size"].height()}, ' + (f'{info["duration"]:.1f} s' if info['duration'] else 'unknown length'))
        self.probed.emit(stamp[0])


def _decode_poster(path, box):
    image = VideoProbe.instance().poster(path)
    if image is None:
        return None
    if box is not None and (image.width() > box[0] or image.height() > box[1]):
        image = image.scaled(int(box[0]), int(box[1]), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return image


if Gst is not None:
    from blink.messagepane.media import register_decoder
    register_decoder(VIDEO_EXTENSIONS, _decode_poster)
