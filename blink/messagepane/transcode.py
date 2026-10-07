"""Turning a recorded WAV into AAC in an .m4a, what mobile records and every client plays.

convert(wav_path, done) runs a GStreamer pipeline (through python3-gi) in the
background: wavparse, audioconvert/audioresample to 16 kHz mono, the first AAC
encoder there is (avenc_aac from gstreamer1.0-libav, else fdkaacenc or
voaacenc) at 32 kbit/s, mp4mux. done(path) is called in the GUI thread with the
.m4a, or with None when there is no GStreamer, no AAC encoder or the pipeline
failed; the caller then sends the WAV. The bus is polled from a Qt timer, so no
GLib main loop is needed.
"""

import os

from PyQt6.QtCore import QObject, QTimer

from blink.logging import ActivityLog


__all__ = ['convert', 'aac_encoder']


try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
except (ImportError, ValueError):
    Gst = None

_initialized = False
_jobs = set()

ENCODERS = (('avenc_aac', {'bitrate': 32000}), ('fdkaacenc', {'bitrate': 32000}), ('voaacenc', {'bitrate': 32000}))


def _init():
    global _initialized
    if Gst is None:
        return False
    if not _initialized:
        Gst.init(None)
        _initialized = True
    return True


def aac_encoder():
    """(factory name, properties) of the AAC encoder to use, or None."""
    if not _init():
        return None
    for name, properties in ENCODERS:
        if Gst.ElementFactory.find(name) is not None:
            return name, properties
    return None


def convert(wav_path, done):
    encoder = aac_encoder()
    missing = [name for name in ('wavparse', 'audioconvert', 'audioresample', 'mp4mux') if Gst is not None and Gst.ElementFactory.find(name) is None]
    if encoder is None or missing:
        reason = 'no python3-gi GStreamer bindings' if Gst is None else f'missing GStreamer elements: {", ".join(missing) or "an AAC encoder (gstreamer1.0-libav)"}'
        ActivityLog().warning(f'[audio] Cannot compress {os.path.basename(wav_path)} to AAC ({reason}), sending it as WAV')
        QTimer.singleShot(0, lambda: done(None))
        return
    _jobs.add(_Job(wav_path, encoder, done))


class _Job(QObject):
    poll_interval = 50      # ms

    def __init__(self, wav_path, encoder, done):
        super().__init__()
        self.wav_path = wav_path
        self.m4a_path = os.path.splitext(wav_path)[0] + '.m4a'
        self.done = done
        name, properties = encoder
        pipeline = Gst.Pipeline.new('voice-note')
        elements = []
        for factory, element_properties in (('filesrc', {'location': wav_path}), ('wavparse', {}), ('audioconvert', {}), ('audioresample', {}),
                                            ('capsfilter', {'caps': Gst.Caps.from_string('audio/x-raw,rate=16000,channels=1')}),
                                            (name, properties), ('mp4mux', {}), ('filesink', {'location': self.m4a_path})):
            element = Gst.ElementFactory.make(factory, None)
            for key, value in element_properties.items():
                element.set_property(key, value)
            pipeline.add(element)
            elements.append(element)
        for first, second in zip(elements, elements[1:]):
            first.link(second)
        self.pipeline = pipeline
        self.bus = pipeline.get_bus()
        self.encoder = name
        self.timer = QTimer(self)
        self.timer.setInterval(self.poll_interval)
        self.timer.timeout.connect(self._poll)
        pipeline.set_state(Gst.State.PLAYING)
        self.timer.start()

    def _poll(self):
        while True:
            message = self.bus.pop_filtered(Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if message is None:
                return
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                ActivityLog().warning(f'[audio] Compressing {os.path.basename(self.wav_path)} to AAC failed ({error.message}), sending it as WAV')
                self._finish(None)
            else:
                size = os.path.getsize(self.m4a_path) if os.path.exists(self.m4a_path) else 0
                if size:
                    ActivityLog().info(f'[audio] {os.path.basename(self.wav_path)} compressed with {self.encoder}: '
                                       f'{os.path.getsize(self.wav_path) // 1024} KB -> {size // 1024} KB')
                self._finish(self.m4a_path if size else None)
            return

    def _finish(self, path):
        self.timer.stop()
        self.pipeline.set_state(Gst.State.NULL)
        if path is None:
            try:
                os.unlink(self.m4a_path)
            except OSError:
                pass
        _jobs.discard(self)
        self.done(path)
