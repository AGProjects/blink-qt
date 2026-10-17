"""Recording a voice note: one recorder for the whole application.

VoiceRecorder.instance() records the default input (16 kHz mono, 16-bit) into
a WAV file named like mobile's recordings (sylk-audio-recording-<ms>.wav, so it
gets their title; it is compressed to AAC .m4a before it is sent, see
blink.messagepane.transcode), measuring as it goes: level() for the meter, the peaks for
the waveform sent with it. At most max_seconds (600); it stops there by itself.
Only one recording at a time, wherever it was started.

While the SIP engine runs, the note is recorded through its audio mixer, from
the input device chosen for calls (only the microphone: what a call plays is
not mixed in), at the mixer's sample rate. The engine holds the sound device
while a call has audio, and capturing the same device with QtMultimedia then
gets nothing. Without the engine (it is not started yet) QtMultimedia records
the default input.
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


def _voice_mixer():
    """The SIP engine's voice mixer when it runs with an input device, else None."""
    try:
        from sipsimple.application import SIPApplication
        mixer = SIPApplication.voice_audio_mixer
    except Exception:
        return None
    if mixer is None or getattr(mixer, 'input_device', None) is None or SIPApplication.voice_audio_device is None:
        return None
    return mixer


def _qt_input_available():
    return QAudioSource is not None and QMediaDevices is not None and not QMediaDevices.defaultAudioInput().isNull()


def recording_available():
    return _voice_mixer() is not None or _qt_input_available()


def wav_samples(path):
    """(sample rate, the 16-bit mono samples) of a PCM WAV file, or (None, empty) when it cannot be read."""
    import wave
    try:
        with wave.open(path, 'rb') as wav:
            if wav.getsampwidth() != 2:
                return None, array('h')
            channels = wav.getnchannels()
            samples = array('h')
            samples.frombytes(wav.readframes(wav.getnframes()))
            if channels > 1:
                samples = samples[::channels]
            return wav.getframerate(), samples
    except (OSError, EOFError, wave.Error):
        return None, array('h')


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
        self.source = None          # QAudioSource, or the SIP engine's WaveRecorder
        self.engine = False         # recording through the SIP engine's mixer
        self._started_at = 0.0
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
        if self.engine:
            return time.monotonic() - self._started_at
        return self._bytes / 2.0 / self.rate

    def _new_path(self):
        directory = ApplicationData.get('voice_notes')
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f'sylk-audio-recording-{int(time.time() * 1000)}.wav')

    def start(self, owner):
        if self.recording or not recording_available():
            return False
        mixer = _voice_mixer()
        if mixer is not None:
            return self._start_engine(owner, mixer)
        return self._start_qt(owner)

    def _start_engine(self, owner, mixer):
        from sipsimple.application import SIPApplication
        from sipsimple.audio import WaveRecorder
        self.path = self._new_path()
        self._input_slot = SIPApplication.voice_audio_device.producer_slot
        recorder = WaveRecorder(mixer, self.path)
        try:
            recorder.start()
            mixer.connect_slots(self._input_slot, recorder.consumer_slot)
        except Exception as e:
            ActivityLog().warning(f'[audio] Cannot record a voice note from {mixer.input_device}: {e}')
            try:
                recorder.stop()
            except Exception:
                pass
            self._remove()
            return False
        self.engine = True
        self.owner = owner
        self.source = recorder
        self._level = 0.0
        self._started_at = time.monotonic()
        self._timer.start()
        ActivityLog().info(f'[audio] Recording a voice note from {mixer.input_device} ({mixer.sample_rate} Hz, through the SIP audio engine)')
        self.started.emit()
        return True

    def _start_qt(self, owner):
        audio_format = QAudioFormat()
        audio_format.setSampleRate(self.rate)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioInput()
        if not device.isFormatSupported(audio_format):
            ActivityLog().warning(f'[audio] {device.description()} cannot record 16 kHz mono 16-bit audio')
            return False
        self.engine = False
        self.path = self._new_path()
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
        if not self.recording:
            return
        if self.engine:
            try:
                tx_level, rx_level = self.source.mixer.get_signal_level(self._input_slot)
                self._level = min(1.0, rx_level / 255.0)       # what the microphone brings into the mixer
            except Exception:
                pass
            if self.seconds >= self.max_seconds:
                self.stop()
                return
        self.progress.emit(self.seconds, self._level)

    def _close(self):
        self._timer.stop()
        if self.source is not None and self.engine:
            recorder = self.source
            try:
                recorder.mixer.disconnect_slots(self._input_slot, recorder.consumer_slot)
            except Exception:
                pass
            try:
                recorder.stop()         # closes the file, with its header
            except Exception as e:
                ActivityLog().warning(f'[audio] Stopping the voice note recorder failed: {e}')
        elif self.source is not None:
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
        engine = self.engine
        if not engine:
            self._read()
            if self._bytes == 0:
                ActivityLog().warning(f'[audio] No sound came from the input device ({self.source.state().name}, {self.source.error().name})')
        self._close()
        if engine:
            rate, samples = wav_samples(self.path)
            duration = len(samples) / rate if rate else 0.0
            self._peaks = array('H')
            if rate:
                per_peak = max(1, int(rate * self.peak_interval))
                for start in range(0, len(samples), per_peak):
                    chunk = samples[start:start + per_peak]
                    self._peaks.append(min(65535, max(abs(min(chunk)), abs(max(chunk)))))
        else:
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
