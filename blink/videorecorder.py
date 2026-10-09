"""Recording a video call from the decoded media, not from the screen.

The remote party's frames are taken where they already are -- the decoded
frames the SDK hands to Python -- through blink.video_frames, so a recording is
independent of the video window: it survives hiding it, docking the video under
the call in the main window, swapping local/remote and full screen. Only the
remote party is recorded; the local camera is not in the picture.

The audio is the session's own call recording (BlinkSession.start_recording),
the conference bridge with both directions already mixed, so it is the same
recording the Record button of an audio call makes and is filed as usual. When
it was already running by hand it is left alone, not stopped with the video.

While the call runs the picture is encoded to H.264 in a scratch .mp4 by a
GStreamer pipeline (appsrc, videoscale into a fixed canvas, the first H.264
encoder there is). At stop the picture is muxed, without re-encoding it, with
the part of the WAV that covers it, compressed to the same AAC a voice note is,
into one .mp4 filed in the conversation with the other party as a movie from
us to us. It stays on this device: it is not sent to anybody.

Threading: frames arrive on the SDK's video thread and are only queued there.
All GStreamer work happens on the recorder's own writer thread, the bus is
read with timed pops there, so no GLib main loop is needed. The queue is
deliberately short: when the encoder cannot keep up the oldest frames are
dropped rather than letting back pressure reach the media thread.

Needs python3-gi with GStreamer, gstreamer1.0-plugins-good (videoscale,
mp4mux, qtdemux, wavparse) and an H.264
encoder: x264enc (gstreamer1.0-plugins-ugly) or openh264enc.
"""

import os
import platform
import queue
import shutil
import struct
import tempfile
import threading
import time
import uuid

from datetime import datetime, timezone

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from zope.interface import implementer

from blink.logging import ActivityLog
from blink.messagepane import transcode
from blink.resources import ApplicationData
from blink.util import run_in_gui_thread


__all__ = ['VideoCallRecorder', 'video_recording_unavailable']


Gst = transcode.Gst

# Frames waiting for the encoder. Short on purpose: a backlog is latency that
# is never recovered, and dropping is better than stalling the video port.
QUEUE_DEPTH = 4

# The canvas is fixed when the pipeline is built. A stream can spend its first
# moments in a size it does not keep (an Android peer opens 656x656 and settles
# on 480x640 within half a second), so a size has to repeat this many times
# before the pipeline is built...
CANVAS_SETTLE_FRAMES = 10

# ...and if it changes anyway while the recording is this young, the movie is
# thrown away and started again at the new size. Later changes are scaled into
# the canvas, with borders where the shape differs.
CANVAS_RESTART_SECONDS = 3.0

MAX_CANVAS_WIDTH = 1920
MAX_CANVAS_HEIGHT = 1080

# The bytes of a decoded frame: what VideoSurface draws as QImage.Format_ARGB32
# on a little endian machine is B, G, R, A in memory; on macOS the SDK gives A, R, G, B.
FRAME_FORMAT = 'ARGB' if platform.system() == 'Darwin' else 'BGRA'

# (factory, properties as strings, bitrate property, bitrate unit in bit/s)
H264_ENCODERS = (('x264enc', {'tune': 'zerolatency', 'speed-preset': 'veryfast', 'key-int-max': '60'}, 'bitrate', 1000),
                 ('openh264enc', {'complexity': 'low'}, 'bitrate', 1))

REQUIRED_ELEMENTS = ('appsrc', 'videoscale', 'videoconvert', 'mp4mux', 'qtdemux', 'filesrc', 'filesink',
                     'wavparse', 'audioconvert', 'audioresample')

AUDIO_RATE = 16000          # what a voice note is
AUDIO_BITRATE = 32000


def _h264_encoder():
    for name, properties, bitrate_property, unit in H264_ENCODERS:
        if Gst.ElementFactory.find(name) is not None:
            return name, properties, bitrate_property, unit
    return None


def video_recording_unavailable():
    """Why a video call cannot be recorded here, or None when it can."""
    if not transcode._init():
        return 'no python3-gi GStreamer bindings'
    missing = [name for name in REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]
    if missing:
        return 'missing GStreamer elements: %s' % ', '.join(missing)
    if _h264_encoder() is None:
        return 'no H.264 encoder (x264enc from gstreamer1.0-plugins-ugly, or openh264enc)'
    return None


def _set(element, properties):
    for key, value in properties.items():
        Gst.util_set_object_arg(element, key, str(value))


@implementer(IObserver)
class VideoCallRecorder(object):
    """One recording of one video call. start() and stop() are called in the GUI thread."""

    def __init__(self, blink_session):
        self.blink_session = blink_session
        self.recording = False
        self.recording_path = None      # the finished movie, in the conversation's file folder

        self._stopping = False
        self._failed = False
        self._workdir = None
        self._video_path = None
        self._filing = None

        self._queue = None
        self._thread = None
        self._subscription = None

        self._pipeline = None
        self._appsrc = None
        self._bus = None
        self._canvas = None
        self._frame_size = None
        self._pending_size = None
        self._pending_count = 0
        self._video_t0 = None
        self._last_pts = None

        self._audio_path = None
        self._audio_t0 = None
        self._own_audio = False

        self._frames_written = 0
        self._frames_dropped = 0

    def log(self, message):
        ActivityLog().info(f'[video] {message}')

    # public API (GUI thread)

    def start(self):
        """Start recording. Returns None, or why it could not start."""
        if self.recording or self._stopping:
            return None
        session = self.blink_session
        reason = video_recording_unavailable()
        if reason is not None:
            return reason
        stream = session.streams.get('video')
        producer = getattr(stream, 'producer', None)
        if producer is None:
            return 'the video stream has no picture yet'
        account = session.account
        if account is None or session.contact_uri is None:
            return 'no conversation to file the recording in'

        from blink.file_transfer import transfer_folder
        from blink.history import conversation_key
        remote_identity = session.sip_session.remote_identity.uri
        user = remote_identity.user.decode() if isinstance(remote_identity.user, bytes) else remote_identity.user
        host = remote_identity.host.decode() if isinstance(remote_identity.host, bytes) else remote_identity.host
        started = datetime.now(timezone.utc)
        party_uri = str(session.contact_uri.uri)
        key = conversation_key(party_uri, account)
        transfer_id = str(uuid.uuid4())
        directory = transfer_folder(ApplicationData.get('file_transfers'), account.id, key, transfer_id)
        name = 'sylk-call-recording-%s-%s@%s-%s.mp4' % (started.astimezone().strftime('%Y%m%d-%H%M%S'), user, host, session.sip_session.direction)
        try:
            self._workdir = tempfile.mkdtemp(prefix='blink-recording-')
        except OSError as e:
            return 'cannot create a scratch directory (%s)' % e
        self.recording_path = os.path.join(directory, name)
        self._video_path = os.path.join(self._workdir, 'video.mp4')
        self._filing = dict(transfer_id=transfer_id, key=key, account=account, uri=party_uri,
                            display_name=session.contact.name if session.contact is not None else '',
                            timestamp=started.replace(tzinfo=None))

        self._queue = queue.Queue(QUEUE_DEPTH)
        self._failed = False
        self._frames_written = self._frames_dropped = 0
        self.recording = True

        self._thread = threading.Thread(target=self._writer_loop, name='VideoCallRecorder', daemon=True)
        self._thread.start()

        self._start_audio()

        from blink import video_frames
        self._subscription = video_frames.subscribe(producer, self._remote_frame)
        if self._subscription is None:
            self.recording = False
            self._stop_audio()
            self._queue.put(None)
            return 'no access to the remote video frames'

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=session, name='BlinkSessionWillEnd')
        notification_center.add_observer(self, sender=session, name='BlinkSessionDidEnd')
        notification_center.add_observer(self, sender=session, name='BlinkSessionDidRemoveStream')
        notification_center.post_notification('BlinkSessionDidChangeRecordingState', sender=session, data=NotificationData(recording=True))
        self.log(f'Started recording the video call with {key} to {self.recording_path}')
        return None

    def stop(self):
        if not self.recording or self._stopping:
            return
        self._stopping = True
        self.recording = False

        notification_center = NotificationCenter()
        for name in ('BlinkSessionWillEnd', 'BlinkSessionDidEnd', 'BlinkSessionDidRemoveStream'):
            notification_center.discard_observer(self, sender=self.blink_session, name=name)

        if self._subscription is not None:
            self._subscription.release()
            self._subscription = None

        self._stop_audio()

        # make room if need be: the sentinel matters more than a frame
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass        # the writer is gone (failed) or about to read: either way it ends
        notification_center.post_notification('BlinkSessionDidChangeRecordingState', sender=self.blink_session, data=NotificationData(recording=self.blink_session.recording))

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionWillEnd(self, notification):
        self.stop()

    _NH_BlinkSessionDidEnd = _NH_BlinkSessionWillEnd

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type in ('video', 'audio'):
            self.stop()

    # audio

    def _start_audio(self):
        session = self.blink_session
        if 'audio' not in session.streams:
            self.log('Recording without audio: this call has no audio stream')
            return
        if session.recording:
            # recording by hand already: leave it alone, do not stop it at our stop, and mux what it records
            self._audio_path = session.recording_path
            self._own_audio = False
            return
        session.start_recording()
        self._audio_path = session.recording_path
        self._audio_t0 = time.monotonic() if self._audio_path else None
        self._own_audio = self._audio_path is not None
        if self._audio_path is None:
            self.log('Recording without audio: the call recording did not start')

    def _stop_audio(self):
        if self._own_audio and self.blink_session.recording:
            self.blink_session.stop_recording()

    # frames (SDK video thread)

    def _remote_frame(self, frame):
        if not self.recording or self._failed:
            return
        item = (frame, time.monotonic())
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # the encoder is behind: drop the oldest frame, the recording stays closer to real time
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass
            self._frames_dropped += 1

    # writer thread

    def _writer_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            frame, timestamp = item
            try:
                self._append(frame, timestamp)
            except Exception as e:
                self._failed = True
                self.log(f'Video recording failed: {e}')
                break
        try:
            self._finish()
        except Exception as e:
            self.log(f'Cannot finish the video recording: {e}')
            self._cleanup()
        self._stopping = False

    def _append(self, frame, timestamp):
        size = (frame.width, frame.height)
        if self._pipeline is None:
            # wait for the size to repeat before committing the canvas to it
            if size != self._pending_size:
                self._pending_size = size
                self._pending_count = 1
                return
            self._pending_count += 1
            if self._pending_count < CANVAS_SETTLE_FRAMES:
                return
            self._open_pipeline(size)
            self._video_t0 = timestamp
        elif size != self._frame_size:
            if timestamp - self._video_t0 < CANVAS_RESTART_SECONDS:
                self.log('Restarting the recording at %dx%d after %.1fs' % (size + (timestamp - self._video_t0,)))
                self._close_pipeline(discard=True)
                self._open_pipeline(size)
                self._video_t0 = timestamp
            else:
                self.log('Remote video is now %dx%d, scaled into the %dx%d recording' % (size + self._canvas))
                self._appsrc.set_property('caps', self._frame_caps(size))
                self._frame_size = size

        data = frame.data
        expected = frame.width * frame.height * 4
        if len(data) < expected:
            self._frames_dropped += 1
            return
        buffer = Gst.Buffer.new_wrapped(bytes(data[:expected]))
        # strictly increasing, whatever the clock says: frames come in bursts
        pts = int((timestamp - self._video_t0) * Gst.SECOND)
        if self._last_pts is not None and pts <= self._last_pts:
            pts = self._last_pts + 1
        self._last_pts = pts
        buffer.pts = pts
        buffer.dts = Gst.CLOCK_TIME_NONE
        buffer.duration = Gst.CLOCK_TIME_NONE
        result = self._appsrc.emit('push-buffer', buffer)
        if result != Gst.FlowReturn.OK:
            raise RuntimeError(f'the encoder refused a frame after {self._frames_written} ({result.value_nick})')
        self._frames_written += 1
        message = self._bus.pop_filtered(Gst.MessageType.ERROR)
        if message is not None:
            error, debug = message.parse_error()
            raise RuntimeError(f'{error.message} after {self._frames_written} frames')

    @staticmethod
    def _frame_caps(size):
        return Gst.Caps.from_string('video/x-raw,format=%s,width=%d,height=%d,framerate=0/1,pixel-aspect-ratio=1/1' % ((FRAME_FORMAT,) + size))

    def _open_pipeline(self, size):
        width = min(size[0], MAX_CANVAS_WIDTH) & ~1
        height = min(size[1], MAX_CANVAS_HEIGHT) & ~1
        if width < 16 or height < 16:
            raise RuntimeError('the remote video is %dx%d' % size)
        name, properties, bitrate_property, unit = _h264_encoder()
        bitrate = max(1000000, min(8000000, width * height * 4))

        pipeline = Gst.Pipeline.new('video-call-recording')
        chain = [('appsrc', {'format': 'time', 'is-live': 'true', 'do-timestamp': 'false', 'block': 'false', 'max-bytes': str(width * height * 4 * 8)}),
                 ('videoscale', {'add-borders': 'true'}),
                 ('capsfilter', None),
                 ('videoconvert', {}),
                 ('capsfilter', None),
                 (name, dict(properties, **{bitrate_property: str(bitrate // unit)})),
                 ('h264parse', {}) if Gst.ElementFactory.find('h264parse') is not None else None,
                 ('mp4mux', {}),
                 ('filesink', {'location': self._video_path})]
        elements = []
        for factory, element_properties in filter(None, chain):
            element = Gst.ElementFactory.make(factory, None)
            if element is None:
                raise RuntimeError(f'cannot create the GStreamer element {factory}')
            if element_properties:
                _set(element, element_properties)
            pipeline.add(element)
            elements.append(element)
        elements[0].set_property('caps', self._frame_caps(size))
        elements[2].set_property('caps', Gst.Caps.from_string('video/x-raw,width=%d,height=%d,pixel-aspect-ratio=1/1' % (width, height)))
        elements[4].set_property('caps', Gst.Caps.from_string('video/x-raw,format=I420'))
        for first, second in zip(elements, elements[1:]):
            if not first.link(second):
                raise RuntimeError(f'cannot link {first.get_factory().get_name()} to {second.get_factory().get_name()}')
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError('the recording pipeline does not start')

        self._pipeline = pipeline
        self._appsrc = elements[0]
        self._bus = pipeline.get_bus()
        self._canvas = (width, height)
        self._frame_size = size
        self._last_pts = None
        self._frames_written = 0
        self.log(f'Recording {width}x{height} with {name} at {bitrate // 1000} kbit/s')

    def _close_pipeline(self, discard=False):
        """End the movie (EOS, so mp4mux writes its index), or throw it away. Whether it was written."""
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is None:
            return False
        written = False
        if not discard:
            self._appsrc.emit('end-of-stream')
            message = self._bus.timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if message is None:
                self.log('Timed out waiting for the movie to be written')
            elif message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                self.log(f'The movie was not written: {error.message}')
            else:
                written = True
        pipeline.set_state(Gst.State.NULL)
        self._appsrc = None
        self._bus = None
        if discard:
            self._remove(self._video_path)
        return written

    def _finish(self):
        if self._pipeline is None:
            self.log('Recording stopped before the remote video settled, nothing kept')
            self._cleanup()
            return
        video_seconds = (self._last_pts or 0) / Gst.SECOND
        written = self._close_pipeline()
        self.log(f'Recorded {self._frames_written} video frames ({self._frames_dropped} dropped), {video_seconds:.1f}s')
        if not written or self._frames_written == 0 or not os.path.getsize(self._video_path):
            self._cleanup()
            return
        try:
            os.makedirs(os.path.dirname(self.recording_path), exist_ok=True)
        except OSError as e:
            self.log(f'Cannot create the folder for the recording: {e}')
            self._cleanup()
            return
        audio = self._audio_excerpt(video_seconds)
        if audio is None or not self._mux(audio):
            self._save_without_audio()
            return
        self._cleanup()
        self._file_in_conversation()

    # audio excerpt and mux (writer thread)

    def _audio_excerpt(self, video_seconds):
        """The part of the call recording that covers the movie, as a WAV of our own in the scratch
        directory, with its lengths right. None when there is nothing to mux."""
        path = self._audio_path
        if not path:
            return None
        # the SDK writes the RIFF and data lengths when it destroys the recorder, which can come late:
        # wait for the file to stop growing and work out the lengths from its size
        size, deadline = -1, time.monotonic() + (5 if self._own_audio else 0)
        while time.monotonic() < deadline:
            try:
                current = os.path.getsize(path)
            except OSError:
                current = -1
            if current == size and (not self._own_audio or current > 44):
                break
            size = current
            time.sleep(0.5)
        read_at = time.monotonic()
        try:
            with open(path, 'rb') as file:
                content = file.read()
        except OSError as e:
            self.log(f'Cannot read the call recording {path}: {e}')
            return None
        if content[:4] != b'RIFF' or content[8:12] != b'WAVE':
            self.log(f'The call recording {path} is not a WAV file')
            return None
        fmt = data_offset = None
        offset = 12
        while offset + 8 <= len(content):
            chunk, length = content[offset:offset+4], struct.unpack('<I', content[offset+4:offset+8])[0]
            if chunk == b'fmt ':
                fmt = struct.unpack('<HHIIHH', content[offset+8:offset+24])
            elif chunk == b'data':
                data_offset = offset + 8
                break
            offset += 8 + length + (length & 1)
        if fmt is None or data_offset is None or fmt[0] != 1:
            self.log('The call recording is not a PCM WAV this can read')
            return None
        audio_format, channels, rate, byte_rate, block, bits = fmt
        data = content[data_offset:]
        data = data[:len(data) - len(data) % block]
        if self._audio_t0 is None:
            # recorded by hand from before the movie, still running: it started its length ago
            self._audio_t0 = read_at - len(data) / byte_rate
        skip = max(0.0, self._video_t0 - self._audio_t0)
        start = int(skip * byte_rate) // block * block
        end = start + int(video_seconds * byte_rate) // block * block
        data = data[start:end]
        if not data:
            self.log(f'The call recording is too short to mux ({skip:.2f}s skipped)')
            return None
        from blink.messagepane.recorder import wav_header
        excerpt = os.path.join(self._workdir, 'audio.wav')
        with open(excerpt, 'wb') as file:
            file.write(wav_header(len(data), rate, channels, bits))
            file.write(data)
        self.log(f'Muxing {len(data) / byte_rate:.2f}s of audio ({skip:.2f}s skipped) into {video_seconds:.2f}s of video')
        return excerpt

    def _mux(self, audio_path):
        encoder = transcode.aac_encoder()
        if encoder is None:
            self.log('No AAC encoder (gstreamer1.0-libav)')
            return False
        name, properties = encoder
        description = ('filesrc name=vsrc ! qtdemux name=demux demux.video_0 ! queue ! mux.video_0 '
                       'filesrc name=asrc ! wavparse ! audioconvert ! audioresample ! audio/x-raw,rate=%d,channels=1 ! %s name=aenc ! queue ! mux.audio_0 '
                       'mp4mux name=mux ! filesink name=sink' % (AUDIO_RATE, name))
        try:
            pipeline = Gst.parse_launch(description)
        except Exception as e:
            self.log(f'Cannot build the mux pipeline: {e}')
            return False
        pipeline.get_by_name('vsrc').set_property('location', self._video_path)
        pipeline.get_by_name('asrc').set_property('location', audio_path)
        pipeline.get_by_name('sink').set_property('location', self.recording_path)
        _set(pipeline.get_by_name('aenc'), dict(properties, bitrate=AUDIO_BITRATE))
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.PLAYING)
        message = bus.timed_pop_filtered(120 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipeline.set_state(Gst.State.NULL)
        if message is None or message.type == Gst.MessageType.ERROR:
            reason = 'timed out' if message is None else message.parse_error()[0].message
            self.log(f'Muxing the recording failed: {reason}')
            self._remove(self.recording_path)
            return False
        if not os.path.exists(self.recording_path) or not os.path.getsize(self.recording_path):
            self.log('Muxing the recording produced nothing')
            return False
        self.log(f'Recording saved to {self.recording_path} ({os.path.getsize(self.recording_path) // 1024} KB, with {name} audio)')
        return True

    def _save_without_audio(self):
        try:
            shutil.move(self._video_path, self.recording_path)
        except OSError as e:
            self.log(f'Cannot save the recording: {e}')
            self._cleanup()
            return
        self.log(f'Recording saved without audio to {self.recording_path}')
        self._cleanup()
        self._file_in_conversation()

    @run_in_gui_thread
    def _file_in_conversation(self):
        """Filed, not sent: an outgoing movie from us to us in the conversation with the other party."""
        from blink.history import MessageHistory
        if not os.path.isfile(self.recording_path):
            return
        MessageHistory().add_call_video_recording(self.recording_path, **self._filing)

    def _remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _cleanup(self):
        workdir, self._workdir = self._workdir, None
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
