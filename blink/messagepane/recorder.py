"""Recording a voice note: one recorder for the whole application.

VoiceRecorder.instance() records the default input (16 kHz mono, 16-bit) into
a WAV file named like mobile's recordings (sylk-audio-recording-<ms>.wav, so it
gets their title; it is compressed to AAC .m4a before it is sent, see
blink.messagepane.transcode), measuring as it goes: level() for the meter, the peaks for
the waveform sent with it. At most max_seconds (600); it stops there by itself.
Only one recording at a time, wherever it was started.
"""

import os
import struct
import time

from array import array

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

try:
    from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
except ImportError:
    QAudioFormat = QAudioSource = QMediaDevices = None

from blink.logging import ActivityLog
from blink.resources import ApplicationData


__all__ = ['VoiceRecorder', 'recording_available', 'wav_header']


def recording_available():
    return QAudioSource is not None and QMediaDevices is not None and not QMediaDevices.defaultAudioInput().isNull()


def wav_header(data_bytes, rate, channels=1, bits=16):
    """The 44-byte header of a PCM WAV file holding data_bytes of samples."""
    block = channels * bits // 8
    return (b'RIFF' + struct.pack('<I', 36 + data_bytes) + b'WAVE' +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, rate, rate * block, block, bits) +
            b'data' + struct.pack('<I', data_bytes))


class VoiceRecorder(QObject):
    started = pyqtSignal()
    progress = pyqtSignal(float, float)         # seconds recorded, level 0..1
    finished = pyqtSignal(object)               # {'path', 'duration', 'peaks'}, or None when cancelled or failed

    rate = 16000
    max_seconds = 600
    peak_interval = 0.05                        # seconds per measured peak

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.owner = None           # whatever started the recording (one at a time)
        self.source = None
        self.path = None
        self._file = None
        self._bytes = 0
        self._peaks = array('H')
        self._chunk = array('h')
        self._level = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    @property
    def recording(self):
        return self.source is not None

    @property
    def seconds(self):
        return self._bytes / 2.0 / self.rate

    def start(self, owner):
        if self.recording or not recording_available():
            return False
        audio_format = QAudioFormat()
        audio_format.setSampleRate(self.rate)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioInput()
        if not device.isFormatSupported(audio_format):
            ActivityLog().warning(f'[audio] {device.description()} cannot record 16 kHz mono 16-bit audio')
            return False
        directory = ApplicationData.get('voice_notes')
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, f'sylk-audio-recording-{int(time.time() * 1000)}.wav')
        self._file = open(self.path, 'wb')
        self._file.write(wav_header(0, self.rate))
        self._bytes = 0
        self._peaks = array('H')
        self._chunk = array('h')
        self._level = 0.0
        self.owner = owner
        self.source = QAudioSource(device, audio_format, self)
        self._io = self.source.start()
        self._io.readyRead.connect(self._read)
        self._timer.start()
        ActivityLog().info(f'[audio] Recording a voice note from {device.description()}')
        self.started.emit()
        return True

    def _read(self):
        data = bytes(self._io.readAll())
        if not data or self._file is None:
            return
        if len(data) % 2:
            data = data[:-1]
        self._file.write(data)
        self._bytes += len(data)
        samples = array('h')
        samples.frombytes(data)
        self._chunk.extend(samples)
        per_peak = int(self.rate * self.peak_interval)
        while len(self._chunk) >= per_peak:
            chunk, self._chunk = self._chunk[:per_peak], self._chunk[per_peak:]
            peak = max(abs(min(chunk)), abs(max(chunk)))
            self._peaks.append(min(65535, peak))
            self._level = min(1.0, peak / 32768.0)
        if self.seconds >= self.max_seconds:
            self.stop()

    def _tick(self):
        if self.recording:
            self.progress.emit(self.seconds, self._level)

    def _close(self):
        self._timer.stop()
        if self.source is not None:
            self.source.stop()
            self.source.deleteLater()
        self.source = None
        if self._file is not None:
            self._file.seek(0)
            self._file.write(wav_header(self._bytes, self.rate))
            self._file.close()
        self._file = None
        self.owner = None

    def stop(self):
        """Finish: finished is emitted with the file, its length and its peaks (0..1, one per 50 ms)."""
        if not self.recording:
            return
        self._read()
        self._close()
        duration = self._bytes / 2.0 / self.rate
        if duration < 0.5:
            ActivityLog().info('[audio] Voice note too short, discarded')
            self._remove()
            self.finished.emit(None)
            return
        top = max(self._peaks) if self._peaks else 0
        peaks = [round(peak / top, 3) if top else 0.0 for peak in self._peaks]
        ActivityLog().info(f'[audio] Voice note recorded: {os.path.basename(self.path)}, {duration:.1f} s')
        self.finished.emit({'path': self.path, 'duration': duration, 'peaks': peaks})

    def cancel(self):
        if not self.recording:
            return
        self._close()
        self._remove()
        ActivityLog().info('[audio] Voice note cancelled')
        self.finished.emit(None)

    def _remove(self):
        try:
            os.unlink(self.path)
        except (OSError, TypeError):
            pass
