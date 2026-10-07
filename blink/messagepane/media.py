"""Media for the transcript, decoded off the GUI thread and cached.

MediaCache.instance().thumbnail(path, box) is what a bubble asks for when it paints: a
QPixmap no larger than box (width, height) keeping the aspect ratio, or None
the first time, in which case the picture is decoded in a worker (QImageReader
scaled while reading, so a 4000 px photo never sits in memory at full size)
and ready(path) is emitted when it can be painted. natural_size(path) reads
only the header. Entries are keyed by path, modification time and size, so a
file replaced on disk is decoded again; they are dropped least recently used
first above byte_limit. Other kinds (a video poster, a PDF page) register a
decoder for their extension with register_decoder().
"""

import os

from collections import OrderedDict

from PyQt6.QtCore import QObject, QRunnable, QSize, QThreadPool, Qt, pyqtSignal
from PyQt6.QtGui import QImageReader, QPixmap

from blink.logging import MessagingTrace as log


__all__ = ['MediaCache', 'register_decoder', 'image_extensions']


def image_extensions():
    return {bytes(name).decode().lower() for name in QImageReader.supportedImageFormats()}


def _decode_image(path, box):
    """QImage of path scaled to fit box, or None. Runs in a worker."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)       # camera pictures stored sideways with an EXIF orientation
    size = reader.size()
    if size.isValid() and box is not None:
        target = size.scaled(QSize(*box), Qt.AspectRatioMode.KeepAspectRatio)
        if target.width() < size.width():
            reader.setScaledSize(target)
    image = reader.read()
    return None if image.isNull() else image


_decoders = {}      # extension: callable(path, box) -> QImage or None, run in a worker


def register_decoder(extensions, decoder):
    for extension in extensions:
        _decoders[extension.lower().lstrip('.')] = decoder


class _Signals(QObject):
    decoded = pyqtSignal(object, object)        # key, QImage or None


class _Job(QRunnable):
    def __init__(self, key, path, box, signals):
        super().__init__()
        self.key, self.path, self.box, self.signals = key, path, box, signals

    def run(self):
        extension = os.path.splitext(self.path)[1].lower().lstrip('.')
        decoder = _decoders.get(extension, _decode_image)
        try:
            image = decoder(self.path, self.box)
        except Exception as e:
            log.warning(f'Cannot decode {self.path}: {e!r}')
            image = None
        self.signals.decoded.emit(self.key, image)


class MediaCache(QObject):
    ready = pyqtSignal(str)             # a path has a picture to paint now

    byte_limit = 96 * 1024 * 1024
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._pixmaps = OrderedDict()   # key: QPixmap
        self._bytes = 0
        self._pending = set()
        self._failed = set()
        self._sizes = {}                # (path, mtime, size): QSize
        self._signals = _Signals()
        self._signals.decoded.connect(self._decoded, Qt.ConnectionType.QueuedConnection)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(2, min(4, QThreadPool.globalInstance().maxThreadCount())))

    @staticmethod
    def _stamp(path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return path, int(stat.st_mtime_ns), stat.st_size

    def natural_size(self, path):
        """The picture's own size (QSize), from its header; invalid when it cannot be read."""
        stamp = self._stamp(path)
        if stamp is None:
            return QSize()
        size = self._sizes.get(stamp)
        if size is None:
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            size = reader.size()
            image_format = bytes(reader.format()).decode().lower()
            if size.isValid() and image_format in ('jpeg', 'jpg', 'heic', 'heif', 'tiff'):
                # a sideways EXIF orientation swaps the shown width and height
                from PyQt6.QtGui import QImageIOHandler
                if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
                    size = size.transposed()
            self._sizes[stamp] = size
        return size

    def thumbnail(self, path, box):
        """The picture fitted in box (w, h) as a QPixmap, or None until it is decoded (ready is emitted)."""
        stamp = self._stamp(path)
        if stamp is None:
            return None
        key = stamp + (int(box[0]), int(box[1]))
        pixmap = self._pixmaps.get(key)
        if pixmap is not None:
            self._pixmaps.move_to_end(key)
            return pixmap
        if key not in self._pending and key not in self._failed:
            self._pending.add(key)
            self._pool.start(_Job(key, path, (int(box[0]), int(box[1])), self._signals))
        return None

    def failed(self, path, box):
        stamp = self._stamp(path)
        return stamp is not None and stamp + (int(box[0]), int(box[1])) in self._failed

    def _decoded(self, key, image):
        self._pending.discard(key)
        if image is None:
            self._failed.add(key)
            return
        pixmap = QPixmap.fromImage(image)
        self._pixmaps[key] = pixmap
        self._bytes += pixmap.width() * pixmap.height() * 4
        while self._bytes > self.byte_limit and len(self._pixmaps) > 1:
            _, dropped = self._pixmaps.popitem(last=False)
            self._bytes -= dropped.width() * dropped.height() * 4
        self.ready.emit(key[0])

    def forget(self, path):
        """Drop every entry of a file (it was deleted or replaced)."""
        for key in [key for key in self._pixmaps if key[0] == path]:
            dropped = self._pixmaps.pop(key)
            self._bytes -= dropped.width() * dropped.height() * 4
        self._failed = {key for key in self._failed if key[0] != path}
