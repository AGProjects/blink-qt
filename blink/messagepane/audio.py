"""Audio for the transcript: one player for the whole application, and waveforms.

AudioPlayer.instance() plays one clip at a time, app-wide: starting another
stops the first, and leaving a conversation does not stop it (the header's ■
does). It tells (changed) which message is playing and where it is, for the
bubbles to draw their progress.

Waveforms come from the recording's peaks companion when the sender made one;
otherwise measure(path) decodes the file once (QAudioDecoder, in the
background, one file at a time) for its duration and 48 bars, kept per file.
Without QtMultimedia both are absent and an audio message is a plain file.
"""

import os

from array import array
from collections import deque

from PyQt6.QtCore import QObject, QUrl, pyqtSignal

try:
    from PyQt6.QtMultimedia import QAudioDecoder, QAudioFormat, QAudioOutput, QMediaPlayer
except ImportError:
    QAudioDecoder = QAudioFormat = QAudioOutput = QMediaPlayer = None

from blink.logging import ActivityLog, MessagingTrace as log
from blink.messagepane.format import waveform_bars


__all__ = ['audio_available', 'AudioPlayer', 'AudioInfo']


def audio_available():
    return QMediaPlayer is not None


class AudioPlayer(QObject):
    changed = pyqtSignal(str)           # the message id whose playback changed ('' when none)

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.message_id = None
        self.path = None
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        self.player.positionChanged.connect(self._SH_Changed)
        self.player.playbackStateChanged.connect(self._SH_Changed)
        self.player.durationChanged.connect(self._SH_Changed)
        self.player.errorOccurred.connect(self._SH_Error)

    @property
    def playing(self):
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def is_current(self, message_id):
        return message_id is not None and message_id == self.message_id

    def fraction(self, message_id):
        """How far the clip of this message is, 0..1, or None when it is not the current one."""
        if not self.is_current(message_id):
            return None
        duration = self.player.duration()
        return min(1.0, self.player.position() / duration) if duration > 0 else 0.0

    def position(self):
        return self.player.position() / 1000.0

    def toggle(self, message_id, path):
        """Play this message's clip, or pause it when it is the one playing."""
        if self.is_current(message_id):
            if self.playing:
                self.player.pause()
            else:
                self.player.play()
            return
        self.player.stop()
        self.message_id, self.path = message_id, path
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        ActivityLog().info(f'[audio] Playing {os.path.basename(path)} of message {message_id}')

    def seek(self, message_id, path, fraction):
        if not self.is_current(message_id):
            self.toggle(message_id, path)
        duration = self.player.duration()
        if duration > 0:
            self.player.setPosition(int(duration * max(0.0, min(1.0, fraction))))
        else:
            self._pending_seek = fraction

    _pending_seek = None

    def stop(self):
        if self.message_id is None:
            return
        self.player.stop()
        message_id, self.message_id, self.path = self.message_id, None, None
        self.changed.emit(message_id)

    def _SH_Changed(self, *args):
        if self._pending_seek is not None and self.player.duration() > 0:
            fraction, self._pending_seek = self._pending_seek, None
            self.player.setPosition(int(self.player.duration() * fraction))
        if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState and self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop()         # played to the end: nothing is current any more
            return
        self.changed.emit(self.message_id or '')

    def _SH_Error(self, error, text):
        if self.message_id is None:
            return          # a later error of a clip already given up on (GStreamer reports the same failure twice)
        hint = ' (an AAC/MP4 voice note needs gstreamer1.0-libav)' if 'plug-in' in text or 'plugin' in text else ''
        ActivityLog().warning(f'[audio] Cannot play {self.path}: {text}{hint}')
        self.stop()


class AudioInfo(QObject):
    """Duration and waveform of audio files, measured once each."""

    measured = pyqtSignal(str)          # a path whose info is known now

    sample_rate = 8000
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.info = {}          # (path, mtime, size): (duration seconds, bars) or None when it cannot be read
        self._queue = deque()
        self._decoder = None
        self._current = None

    @staticmethod
    def _stamp(path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return path, int(stat.st_mtime_ns), stat.st_size

    def get(self, path):
        """(duration, bars) of a file, or None until it is measured (measured is emitted); measuring starts here."""
        stamp = self._stamp(path)
        if stamp is None or QAudioDecoder is None:
            return None
        if stamp in self.info:
            return self.info[stamp]
        if stamp not in self._queue and stamp != self._current:
            self._queue.append(stamp)
            self._next()
        return None

    def _next(self):
        if self._decoder is not None or not self._queue:
            return
        self._current = self._queue.popleft()
        self._peaks = array('H')
        self._seconds = 0.0
        decoder = self._decoder = QAudioDecoder(self)
        audio_format = QAudioFormat()
        audio_format.setSampleRate(self.sample_rate)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        decoder.setAudioFormat(audio_format)
        decoder.bufferReady.connect(self._SH_BufferReady)
        decoder.finished.connect(self._SH_Finished)
        decoder.error.connect(self._SH_Error)
        decoder.setSource(QUrl.fromLocalFile(self._current[0]))
        decoder.start()

    def _SH_BufferReady(self):
        buffer = self._decoder.read()
        if not buffer.isValid():
            return
        audio_format = buffer.format()
        if audio_format.sampleFormat() != QAudioFormat.SampleFormat.Int16:
            return                  # the decoder did not convert: nothing to measure from this buffer
        data = array('h')
        data.frombytes(buffer.constData().asstring(buffer.byteCount()))
        # 20 ms per peak, at the rate the decoder actually delivers (it may not honour the one asked for)
        step = max(1, audio_format.sampleRate() * max(1, audio_format.channelCount()) // 50)
        self._seconds += len(data) / float(max(1, audio_format.sampleRate() * max(1, audio_format.channelCount())))
        for start in range(0, len(data), step):
            chunk = data[start:start + step]
            if chunk:
                self._peaks.append(min(65535, max(abs(min(chunk)), abs(max(chunk)))))

    def _done(self, result):
        stamp = self._current
        self.info[stamp] = result
        decoder, self._decoder, self._current = self._decoder, None, None
        decoder.deleteLater()
        self.measured.emit(stamp[0])
        self._next()

    def _SH_Finished(self):
        duration = self._seconds
        self._done((duration, waveform_bars(self._peaks)) if self._peaks else None)

    def _SH_Error(self, error):
        log.warning(f'Cannot measure {self._current[0]}: {self._decoder.errorString()}')
        self._done(None)
