"""Extra videos of ours in a video room: a second camera, or an RTSP camera (CCTV).

Each is a publisher of its own in the room, video only, joined over the room's sylkRTC
connection with `parent` (our own publisher's session id) and `label` ("Camera 1"): the
gateway shows it under us, not as another participant, and gives it no chat leg. Its
pipeline is complete before the offer, with the room's codec (the one the answer of our own
publisher chose), so the offer carries its SSRC:

  source ─► decodebin ─► convert/scale/rate (640x360 by default, 10 fps) ─► tee ─┬─► encoder ─► payloader ─► webrtcbin
                                                                      └─► appsink ─► its tile here

Sources: rtspsrc (location, user, password; video only) or a local camera from GStreamer's
device monitor. An RTSP source that worked is remembered with its password in ui.conf
(`videoroom/video_sources`) and offered again.
"""

import json
import random
import re
import uuid

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QImage

from blink.logging import ActivityLog
from blink.util import call_in_gui_thread
from blink.webrtc.videoroom import Gst, GstWebRTC, Participant, VIDEO_CODECS, _make, _has, _sdp_message, _video_ssrc


__all__ = ['ExtraVideo', 'RESOLUTIONS', 'DEFAULT_RESOLUTION', 'local_cameras', 'saved_sources', 'remember_source', 'rename_source', 'set_source_resolution', 'forget_source']


FPS = 10
# what an extra video is sent at: (name, width, height, kbit/s), 16:9; the source is scaled to it
RESOLUTIONS = [('1280x720', 1280, 720, 1200), ('640x360', 640, 360, 500), ('320x180', 320, 180, 200)]
DEFAULT_RESOLUTION = '640x360'


def _resolution(name):
    return next((resolution for resolution in RESOLUTIONS if resolution[0] == name), None) or \
           next(resolution for resolution in RESOLUTIONS if resolution[0] == DEFAULT_RESOLUTION)
JOIN_TIMEOUT = 25000    # ms from the offer to established
SOURCES_KEY = 'videoroom/video_sources'


# Remembered sources

def saved_sources():
    """The RTSP sources that worked, newest first: [{label, url, user, password, resolution}]."""
    from blink.resources import ui_settings
    try:
        sources = json.loads(ui_settings().value(SOURCES_KEY, '[]') or '[]')
    except (TypeError, ValueError):
        sources = []
    return [source for source in sources if isinstance(source, dict) and source.get('url')]


def remember_source(source):
    from blink.resources import ui_settings
    previous = next((other for other in saved_sources() if other.get('url') == source.get('url')), {})
    sources = [other for other in saved_sources() if other.get('url') != source.get('url')]
    sources.insert(0, dict(label=source.get('label') or '', url=source['url'], user=source.get('user') or '', password=source.get('password') or '',
                           resolution=source.get('resolution') or previous.get('resolution') or DEFAULT_RESOLUTION))
    ui_settings().setValue(SOURCES_KEY, json.dumps(sources[:20]))


def rename_source(url, label):
    """A new name for a remembered source, where it is in the list."""
    from blink.resources import ui_settings
    sources = saved_sources()
    for source in sources:
        if source.get('url') == url:
            source['label'] = label
    ui_settings().setValue(SOURCES_KEY, json.dumps(sources))


def set_source_resolution(url, resolution):
    """The resolution a remembered source is sent at, where it is in the list."""
    from blink.resources import ui_settings
    sources = saved_sources()
    for source in sources:
        if source.get('url') == url:
            source['resolution'] = resolution
    ui_settings().setValue(SOURCES_KEY, json.dumps(sources))


def forget_source(url):
    from blink.resources import ui_settings
    ui_settings().setValue(SOURCES_KEY, json.dumps([source for source in saved_sources() if source.get('url') != url]))


def local_cameras():
    """[(name, device)] of the cameras GStreamer's device monitor sees (device: a Gst.Device)."""
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter('Video/Source', None)
    try:
        monitor.start()
        devices = monitor.get_devices() or []
    finally:
        monitor.stop()
    def api(device):
        properties = device.get_properties()
        return properties.get_value('device.api') if properties is not None and properties.has_field('device.api') else None
    # a camera can come both through v4l2 and PipeWire: v4l2 when there is any (the SDK's cameras are v4l2)
    v4l2 = [device for device in devices if api(device) != 'pipewire']
    return [(device.get_display_name(), device) for device in (v4l2 or devices)]


def _video_stream_only(rtspsrc, number, caps):
    """rtspsrc select-stream: the video streams only (a camera's audio would be an unlinked pad, which stops rtspsrc)."""
    return caps is not None and caps.get_size() > 0 and caps.get_structure(0).get_string('media') == 'video'


def _describe(source):
    if source['kind'] == 'rtsp':
        return source['url']
    return source.get('name') or 'camera'


# One extra video

class ExtraVideo(QObject):
    stateChanged = pyqtSignal(str, str)     # state (connecting, established, ended), reason

    def __init__(self, session, source, label):
        """source: dict(kind='rtsp', url, user, password) or dict(kind='camera', name, device)."""
        super(ExtraVideo, self).__init__()
        self.session = session
        self.source = source
        self.label = label
        self.id = str(uuid.uuid4())
        self.state = 'idle'
        self.pipeline = None
        self.webrtcbin = None
        self.ssrc = random.randint(1, 0xffffffff)
        self.resolution = _resolution(source.get('resolution') or DEFAULT_RESOLUTION)[0]
        self._caps = self._encoder = self._encoder_factory = None
        # the tile here: an ordinary participant of the window, its frames from this pipeline
        self.participant = Participant(dict(id=self.id, uri=session.identity, display_name=label, type='webrtc', parent=session.id, label=label))
        self.participant.session = session
        self.participant.video = True
        self.participant.state = 'connecting'
        self.participant.extra_video = self
        self._join_timer = QTimer(self)
        self._join_timer.setSingleShot(True)
        self._join_timer.setInterval(JOIN_TIMEOUT)
        self._join_timer.timeout.connect(lambda: self._end('no media connection'))

    def __repr__(self):
        return f'<ExtraVideo {self.label} {_describe(self.source)} {self.state}>'

    def log(self, text, level='info'):
        getattr(ActivityLog(), level)(f'[videoroom] {self.session.room}: {self.label}: {text}')

    # Start and stop

    def start(self):
        session = self.session
        if session.connection is None or session.state != 'established' or not session.video_codec:
            raise RuntimeError('the room is not connected yet')
        codec = next(((name, pt, encoder, payloader) for name, pt, encoder, payloader in VIDEO_CODECS
                      if name == session.video_codec and pt == session.video_pt and _has(encoder) and _has(payloader)), None)
        if codec is None:
            raise RuntimeError(f'no encoder for the room\'s {session.video_codec}')
        self.state = 'connecting'
        session.extra_videos[self.id] = self
        session.connection.received.connect(self._SH_ConnectionEvent)
        self._build(codec)
        name, width, height, bitrate = _resolution(self.resolution)
        self.log(f'adding {_describe(self.source)} ({codec[0]} {width}x{height} {FPS} fps, {bitrate} kbit/s)')
        self.stateChanged.emit('connecting', '')

    def stop(self, reason='stopped'):
        if self.state in ('idle', 'ended'):
            return
        connection = self.session.connection
        if connection is not None and connection.state == 'ready' and self.state != 'connecting-failed':
            connection.request(dict(sylkrtc='videoroom-leave', session=self.id))
        self._end(reason)

    def _end(self, reason):
        if self.state == 'ended':
            return
        self.state = 'ended'
        self._join_timer.stop()
        try:
            self.session.connection.received.disconnect(self._SH_ConnectionEvent)
        except (TypeError, AttributeError):
            pass
        self.session.extra_videos.pop(self.id, None)
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            pipeline.get_bus().set_sync_handler(None, None)
            pipeline.set_state(Gst.State.NULL)
        self.participant.state = 'failed' if reason not in ('stopped', 'left') else 'ended'
        self.log(f'ended: {reason}')
        self.stateChanged.emit('ended', reason)

    # The pipeline

    def _build(self, codec):
        name, pt, encoder_factory, payloader_factory = codec
        session = self.session
        self.pipeline = pipeline = Gst.Pipeline.new('extra-video')
        pipeline.get_bus().set_sync_handler(self._on_bus_message, None)

        if self.source['kind'] == 'rtsp':
            source = _make('rtspsrc', 'source', location=self.source['url'], latency=200, protocols='tcp')
            if self.source.get('user'):
                source.set_property('user-id', self.source['user'])
                source.set_property('user-pw', self.source.get('password') or '')
        else:
            source = self.source['device'].create_element('source')
        decodebin = _make('decodebin')
        convert, scale, rate = _make('videoconvert'), _make('videoscale', add_borders=True), _make('videorate', drop_only=True)
        resolution, width, height, bitrate = _resolution(self.resolution)
        caps = self._caps = _make('capsfilter', caps=self._raw_caps(width, height))
        tee = _make('tee')
        # to the room
        if encoder_factory in ('vp8enc', 'vp9enc'):
            encoder = _make(encoder_factory, deadline=1, cpu_used=8, target_bitrate=bitrate * 1000, keyframe_max_dist=FPS * 4,
                            end_usage='cbr', lag_in_frames=0, error_resilient='default')
        elif encoder_factory == 'x264enc':
            encoder = _make('x264enc', tune='zerolatency', speed_preset='ultrafast', bitrate=bitrate, key_int_max=FPS * 4, bframes=0)
        else:
            encoder = _make('openh264enc', bitrate=bitrate * 1000, gop_size=FPS * 4)
        self._encoder, self._encoder_factory = encoder, encoder_factory
        send = [_make('queue', max_size_buffers=2, leaky='downstream'), encoder]
        if encoder_factory in ('x264enc', 'openh264enc'):
            send.append(_make('capsfilter', caps=Gst.Caps.from_string('video/x-h264,profile=constrained-baseline')))
        send.append(_make(payloader_factory, pt=pt, mtu=1200, config_interval=-1, ssrc=self.ssrc))
        send.append(_make('capsfilter', caps=Gst.Caps.from_string(f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000')))
        # to its tile here
        sink = _make('appsink', emit_signals=True, max_buffers=1, drop=True, sync=False, caps=Gst.Caps.from_string('video/x-raw,format=BGRx'))
        sink.connect('new-sample', self._on_preview_sample)
        preview = [_make('queue', max_size_buffers=1, leaky='downstream'), _make('videoconvert'), sink]

        self.webrtcbin = webrtcbin = _make('webrtcbin', 'extra-publisher', bundle_policy='max-bundle')
        session._configure_ice(webrtcbin)
        for element in [source, decodebin, convert, scale, rate, caps, tee, webrtcbin] + send + preview:
            pipeline.add(element)
        for chain in ([convert, scale, rate, caps, tee], send, preview):
            for left, right in zip(chain, chain[1:]):
                if not left.link(right):
                    raise RuntimeError(f'cannot link {left.get_name()} to {right.get_name()}')
        tee.link(send[0])
        tee.link(preview[0])
        pad = webrtcbin.request_pad_simple('sink_%u')
        transceiver = self._transceiver = pad.get_property('transceiver')
        self._codec = (name, pt)
        transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        preferences = f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000,rtcp-fb-nack=(boolean)true,rtcp-fb-nack-pli=(boolean)true,rtcp-fb-ccm-fir=(boolean)true,ssrc=(uint){self.ssrc}'
        if name == 'H264':
            preferences += ',packetization-mode=(string)1,profile-level-id=(string)42e01f,level-asymmetry-allowed=(string)1'
        transceiver.set_property('codec-preferences', Gst.Caps.from_string(preferences))
        if send[-1].get_static_pad('src').link(pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError('cannot link the video to webrtcbin')

        def decoded(element, decoded_pad):
            caps = decoded_pad.get_current_caps() or decoded_pad.query_caps(None)
            if caps is not None and caps.get_size() and caps.get_structure(0).get_name().startswith('video/'):
                decoded_pad.link(convert.get_static_pad('sink'))
        decodebin.connect('pad-added', decoded)
        if self.source['kind'] == 'rtsp':
            def rtsp_pad(element, rtsp_pad_):
                caps = rtsp_pad_.get_current_caps() or rtsp_pad_.query_caps(None)
                media = caps.get_structure(0).get_string('media') if caps is not None and caps.get_size() else None
                if media == 'video' and not decodebin.get_static_pad('sink').is_linked():
                    rtsp_pad_.link(decodebin.get_static_pad('sink'))
                else:
                    discard(rtsp_pad_)
            def discard(pad):
                # anything else that still comes (a second video stream): an unlinked pad would stop rtspsrc (not-linked)
                fakesink = _make('fakesink', sync=False)
                fakesink.set_property('async', False)
                pipeline.add(fakesink)
                fakesink.sync_state_with_parent()
                pad.link(fakesink.get_static_pad('sink'))
            source.connect('select-stream', _video_stream_only)     # the camera's audio (and metadata) are not even set up
            source.connect('pad-added', rtsp_pad)
        else:
            source.link(decodebin)

        webrtcbin.connect('on-negotiation-needed', lambda element: None)
        webrtcbin.connect('on-ice-candidate', session._on_ice_candidate, self.id)
        webrtcbin.connect('notify::ice-gathering-state', session._on_gathering_state, self.id)
        webrtcbin.connect('notify::ice-connection-state', self._on_ice_connection_state)
        pipeline.set_state(Gst.State.PLAYING)
        webrtcbin.emit('create-offer', None, Gst.Promise.new_with_change_func(self._on_offer_created, None))
        self._join_timer.start()

    @staticmethod
    def _raw_caps(width, height):
        return Gst.Caps.from_string(f'video/x-raw,format=I420,width={width},height={height},framerate={FPS}/1,pixel-aspect-ratio=1/1')

    @property
    def size(self):
        resolution, width, height, bitrate = _resolution(self.resolution)
        return width, height

    def set_resolution(self, resolution):
        """Send at another resolution (one of RESOLUTIONS), renegotiated in place; an RTSP camera keeps it."""
        resolution, width, height, bitrate = _resolution(resolution)
        if resolution == self.resolution:
            return
        self.resolution = resolution
        if self.source['kind'] == 'rtsp':
            self.source['resolution'] = resolution
            set_source_resolution(self.source['url'], resolution)
        if self._caps is None:
            return
        self._caps.set_property('caps', self._raw_caps(width, height))
        encoder = self._encoder
        if self._encoder_factory in ('vp8enc', 'vp9enc'):
            encoder.set_property('target-bitrate', bitrate * 1000)
        elif self._encoder_factory == 'x264enc':
            encoder.set_property('bitrate', bitrate)
        elif encoder.find_property('bitrate') is not None:
            encoder.set_property('bitrate', bitrate * 1000)
        self.log(f'sending at {width}x{height}, {bitrate} kbit/s')
        self.participant.changed.emit()

    def _on_offer_created(self, promise, data):
        # streaming thread
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        if offer is None:
            call_in_gui_thread(self._end, 'cannot create the offer')
            return
        self.webrtcbin.emit('set-local-description', offer, Gst.Promise.new())
        self._narrow_codec_preferences()
        call_in_gui_thread(self._send_join, offer.sdp.as_text())

    def _narrow_codec_preferences(self):
        """The offer carried the codec's fmtp (profile-level-id 42e01f): the encoder's caps do not match
        those (x264's constrained baseline is 42c0..), and webrtcbin would refuse its packets (not-negotiated).
        As our own publisher does after the answer, the preferences are narrowed to the codec and the SSRC."""
        name, pt = self._codec
        caps = f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000,ssrc=(uint){self.ssrc}'
        self._transceiver.set_property('codec-preferences', Gst.Caps.from_string(caps))

    def _send_join(self, sdp):
        if self.state != 'connecting':
            return
        session = self.session
        if _video_ssrc(sdp) is None:
            self.log('the offer has no SSRC: the server may drop the video', 'warning')
        session.connection.request(dict(sylkrtc='videoroom-join', account=session.identity, session=self.id, uri=session.room,
                                        sdp=sdp, audio=False, video=True, parent=session.id, label=self.label), self._join_answered)
        session._flush_trickle(self.id)

    def _join_answered(self, error):
        if error is not None:
            self.state = 'connecting-failed'
            self._end(f'the server refused it: {error}')

    def _on_preview_sample(self, sink):
        # streaming thread: the picture for its tile here
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value('width'), caps.get_value('height')
        buffer = sample.get_buffer()
        ok, mapping = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                image = QImage(bytes(mapping.data), width, height, width * 4, QImage.Format.Format_RGB32).copy()
            finally:
                buffer.unmap(mapping)
            self.participant.frameReady.emit(image)
        return Gst.FlowReturn.OK

    def _on_ice_connection_state(self, webrtcbin, spec):
        if webrtcbin.get_property('ice-connection-state') == GstWebRTC.WebRTCICEConnectionState.FAILED:
            call_in_gui_thread(self.stop, 'the media connection failed (ICE)')

    def _on_bus_message(self, bus, message, data):
        # any thread
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source = message.src.get_name() if message.src is not None else '?'
            ActivityLog().info(f'[videoroom] {self.session.room}: {self.label}: GStreamer error in {source}: {error.message} ({debug})')
            refused = source == 'source' and re.search(r'unauthori[sz]ed|401|not found|404|could not (open|connect)|resource', f'{error.message} {debug}', re.I)
            call_in_gui_thread(self.stop, f'{error.message}' + (' (check the address, user and password)' if refused else ''))
        return Gst.BusSyncReply.DROP

    # Signalling

    def _SH_ConnectionEvent(self, message):
        if message.get('sylkrtc') != 'videoroom-event' or message.get('session') != self.id:
            return
        if message.get('event') != 'session-state':
            return      # the room's publishers and the rest: our own session has them
        state = message.get('state')
        if state == 'accepted':
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, _sdp_message(message.get('sdp') or ''))
            self.webrtcbin.emit('set-remote-description', answer, Gst.Promise.new())
            self._narrow_codec_preferences()
        elif state == 'established':
            self._join_timer.stop()
            self.state = 'established'
            self.participant.state = 'established'
            self.participant.changed.emit()
            self.log('sending')
            if self.source['kind'] == 'rtsp':
                remember_source(dict(label=self.label, url=self.source['url'], user=self.source.get('user'), password=self.source.get('password'), resolution=self.resolution))
            self.stateChanged.emit('established', '')
        elif state == 'terminated':
            self.state = 'connecting-failed'    # nothing to leave
            self._end(message.get('reason') or 'ended by the server')

