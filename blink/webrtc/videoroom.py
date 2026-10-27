"""Joining a SylkServer video conference room over WebRTC.

SylkServer rooms are a Janus videoroom (an SFU) behind the webrtcgateway: every
participant publishes its camera and microphone on one PeerConnection and
subscribes to each other publisher on one PeerConnection per publisher (a feed).
The signalling is sylkRTC (blink.webrtc.sylkrtc): videoroom-join with our offer, the
answer in session-state accepted, the publishers in initial-publishers and
publishers-joined/left, then per publisher videoroom-feed-attach, the server's
offer in feed-attached and our videoroom-feed-answer.

The media is GStreamer webrtcbin, all in one pipeline so the echo canceller sees
what is played (webrtcdsp/webrtcechoprobe allow one probe per process):

    camera (SDK frames) -> appsrc -> convert/scale/rate -> encoder -> payloader -+
    microphone -> webrtcdsp -> volume -> opusenc -> rtpopuspay -----------------+-> webrtcbin (publisher)

    webrtcbin (feed) -> decodebin -+-> video: convert -> appsink -> the tile
                                   +-> audio: convert/resample -> audiomixer -> webrtcechoprobe -> speakers

The camera is the SDK's (SIPApplication.video_device), read through a frame
renderer like any other view of it, so the camera chosen in Blink, the screen
included, is what the room sees.

The room's video codec is set on the server (Janus videocodec), so the publisher
offers every codec it can encode and links the encoder for the one the answer
picked.

Publishers of type sip are callers behind the room's audio bridge: they have no
feed, they are shown by name only. The bridge itself (type bridge) carries their
mixed audio: it is subscribed to, but not shown.

Threads: GStreamer calls back on its streaming threads; anything that touches the
signalling or the user interface is handed to the GUI thread.
"""

import re
import uuid
from time import monotonic

from urllib.parse import quote

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QTransform

from application.notification import NotificationCenter, NotificationData
from application.python.types import Singleton

from sipsimple.account import BonjourAccount

from blink.logging import ActivityLog
from blink.webrtc import ws_server
from blink.webrtc.sylkrtc import SylkRTCConnections, websockets_available
from blink.util import call_in_gui_thread


__all__ = ['VideoroomManager', 'VideoroomSession', 'Participant', 'available', 'media_missing']


try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
except (ImportError, ValueError) as e:
    Gst = None
    _gi_error = str(e)
else:
    _gi_error = None

GstSdp = GstWebRTC = None
if Gst is not None:
    try:
        gi.require_version('GstWebRTC', '1.0')
        gi.require_version('GstSdp', '1.0')
        from gi.repository import GstSdp, GstWebRTC
    except (ImportError, ValueError) as e:
        GstSdp = GstWebRTC = None
        _gi_error = str(e)


VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS = 640, 480, 24     # as the SIP video calls (VGA, 24 fps)
VIDEO_BITRATE = 1024    # kbit/s
SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_FPS = 1280, 720, 15    # a shared screen: its text readable, less motion
SCREEN_BITRATE = 1800   # kbit/s
# The screen is sent at its own size: the encoder reopens at 720p mid-stream (False: scaled into the camera's)
SCREEN_CHANGES_SIZE = True

# What the publisher can offer, in order of preference: (encoding name, payload type, encoder factory, payloader factory)
VIDEO_CODECS = [
    ('VP9', 98, 'vp9enc', 'rtpvp9pay'),
    ('VP8', 96, 'vp8enc', 'rtpvp8pay'),
    ('H264', 102, 'x264enc', 'rtph264pay'),
    ('H264', 102, 'openh264enc', 'rtph264pay'),
]
OPUS_PT = 111

REQUIRED_ELEMENTS = ['webrtcbin', 'nicesrc', 'appsrc', 'appsink', 'decodebin', 'videoconvert', 'videoscale', 'videorate',
                     'audioconvert', 'audioresample', 'audiomixer', 'volume', 'opusenc', 'rtpopuspay', 'autoaudiosrc', 'autoaudiosink']

PUBLISHER_TIMEOUT = 20 * 1000   # ms from join to established


def _init():
    if _gi_error is not None:
        return False
    if not Gst.is_initialized():
        Gst.init([])
    return True


def _has(factory):
    return Gst.ElementFactory.find(factory) is not None


# The Debian/Ubuntu package that provides each element
ELEMENT_PACKAGES = {
    'webrtcbin': 'gstreamer1.0-plugins-bad', 'webrtcdsp': 'gstreamer1.0-plugins-bad', 'webrtcechoprobe': 'gstreamer1.0-plugins-bad',
    'audiomixer': 'gstreamer1.0-plugins-base', 'openh264enc': 'gstreamer1.0-plugins-bad',
    'nicesrc': 'gstreamer1.0-nice',
    'appsrc': 'gstreamer1.0-plugins-base', 'appsink': 'gstreamer1.0-plugins-base', 'decodebin': 'gstreamer1.0-plugins-base',
    'videoconvert': 'gstreamer1.0-plugins-base', 'videoscale': 'gstreamer1.0-plugins-base', 'videorate': 'gstreamer1.0-plugins-base',
    'audioconvert': 'gstreamer1.0-plugins-base', 'audioresample': 'gstreamer1.0-plugins-base', 'volume': 'gstreamer1.0-plugins-base',
    'opusenc': 'gstreamer1.0-plugins-base', 'rtpopuspay': 'gstreamer1.0-plugins-good',
    'autoaudiosrc': 'gstreamer1.0-plugins-good', 'autoaudiosink': 'gstreamer1.0-plugins-good',
    'vp9enc': 'gstreamer1.0-plugins-good', 'vp8enc': 'gstreamer1.0-plugins-good', 'rtpvp9pay': 'gstreamer1.0-plugins-good',
    'rtpvp8pay': 'gstreamer1.0-plugins-good', 'rtph264pay': 'gstreamer1.0-plugins-good', 'x264enc': 'gstreamer1.0-plugins-ugly',
}


def media_missing():
    """What is missing for WebRTC media, as a list of (what, Debian package); empty when everything is there."""
    missing = [] if websockets_available() else [('Qt WebSockets', 'python3-pyqt6.qtwebsockets')]
    if Gst is None:
        return missing + [('GStreamer introspection (%s)' % _gi_error, 'gir1.2-gstreamer-1.0')]
    if not _init():
        return missing + [('GStreamer WebRTC introspection (%s)' % _gi_error, 'gir1.2-gst-plugins-bad-1.0')]
    missing += [(name, ELEMENT_PACKAGES.get(name, 'gstreamer1.0-plugins-good')) for name in REQUIRED_ELEMENTS if not _has(name)]
    if not any(_has(encoder) and _has(payloader) for name, pt, encoder, payloader in VIDEO_CODECS):
        missing.append(('a video encoder (vp9enc, vp8enc, x264enc or openh264enc)', 'gstreamer1.0-plugins-good'))
    return missing


def media_report():
    """One line about the WebRTC media that can be done here, for the Activity log."""
    if not _init():
        return 'no GStreamer WebRTC'
    parts = ['GStreamer %s' % Gst.version_string().replace('GStreamer ', '')]
    encoders = ['%s (%s)' % (name, encoder) for name, pt, encoder, payloader in VIDEO_CODECS if _has(encoder) and _has(payloader)]
    parts.append('video %s' % (', '.join(encoders) or 'none'))
    parts.append('audio OPUS')
    parts.append('echo cancellation %s' % ('webrtcdsp' if _has('webrtcdsp') and _has('webrtcechoprobe') else 'none'))
    parts.append('Qt WebSockets %s' % ('yes' if websockets_available() else 'no'))
    return ', '.join(parts)


_media_checked = None


def available(account):
    """Whether account can join video rooms: a SylkServer domain with a wsServer, and the media elements."""
    global _media_checked
    if account is None or account is BonjourAccount() or ws_server(account) is None:
        return False
    if _media_checked is None:
        missing = media_missing()
        _media_checked = not missing
        if missing:
            ActivityLog().warning(f'[videoroom] WebRTC video conferences are not available, missing: {", ".join(what for what, package in missing)}')
    return _media_checked


def _make(factory, name=None, **properties):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f'GStreamer element {factory} is missing')
    for key, value in properties.items():
        key = key.replace('_', '-')
        if element.find_property(key) is None:
            continue    # a property this version does not have
        if isinstance(value, (str, bool, int, float)) and not isinstance(value, Gst.Caps):
            # strings take enum and flags nicks; one path for every type keeps versions apart
            Gst.util_set_object_arg(element, key, str(value).lower() if isinstance(value, bool) else str(value))
        else:
            element.set_property(key, value)
    return element


def _ice_urls(ice_servers):
    """(stun-server, [turn-server URLs]) for webrtcbin from a WebRTC iceServers list."""
    stun, turn = None, []
    for server in ice_servers or []:
        if not isinstance(server, dict):
            continue
        urls = server.get('urls') or server.get('url') or []
        if isinstance(urls, str):
            urls = [urls]
        for url in urls:
            if not isinstance(url, str):
                continue
            scheme, _, rest = url.partition(':')
            scheme = scheme.lower()
            rest = rest.lstrip('/')
            if scheme == 'stun' and stun is None:
                stun = 'stun://' + rest.partition('?')[0]
            elif scheme in ('turn', 'turns'):
                host, _, query = rest.partition('?')
                credentials = ''
                if server.get('username'):
                    credentials = '%s:%s@' % (quote(str(server['username']), safe=''), quote(str(server.get('credential', '')), safe=''))
                turn.append('%s://%s%s%s' % (scheme, credentials, host, ('?' + query) if query else ''))
    return stun, turn


def _sdp_message(text):
    result, message = GstSdp.SDPMessage.new_from_text(text)
    if result != GstSdp.SDPResult.OK:
        raise ValueError('unreadable SDP')
    return message


def _media_summary(sdp):
    """'audio OPUS/48000, video VP9/90000' from an SDP, for the Activity log."""
    parts = []
    for match in re.finditer(r'^m=(\w+) (\d+) [^ ]+ ([^\r\n]*)', sdp, re.M):
        media, port, payloads = match.group(1), match.group(2), match.group(3).split()
        section_end = re.search(r'^m=', sdp[match.end():], re.M)
        section = sdp[match.end():match.end() + section_end.start()] if section_end else sdp[match.end():]
        if port == '0' and 'a=bundle-only' not in section:
            parts.append(f'{media} rejected')
            continue
        codec = None
        if payloads:
            rtpmap = re.search(r'^a=rtpmap:%s ([^\r\n]+)' % re.escape(payloads[0]), sdp[match.end():], re.M)
            codec = rtpmap.group(1) if rtpmap else payloads[0]
        parts.append(f'{media} {codec}' if codec else media)
    return ', '.join(parts) or 'no media'


def _offers_video(sdp):
    """Whether a feed's offer carries video: an m=video that is not rejected nor inactive."""
    for match in re.finditer(r'^m=video (\d+) [^\r\n]*', sdp, re.M):
        section_end = re.search(r'^m=', sdp[match.end():], re.M)
        section = sdp[match.end():match.end() + section_end.start()] if section_end else sdp[match.end():]
        if match.group(1) == '0' and 'a=bundle-only' not in section:
            continue
        if re.search(r'^a=(inactive|recvonly)\s*$', section, re.M):
            continue
        return True
    return False


def _video_ssrc(sdp):
    """The first a=ssrc of the m=video section of an SDP (ours), or None."""
    match = re.search(r'^m=video [^\r\n]*', sdp, re.M)
    if match is None:
        return None
    rest = sdp[match.end():]
    next_media = re.search(r'^m=', rest, re.M)
    if next_media is not None:
        rest = rest[:next_media.start()]
    ssrc = re.search(r'^a=ssrc:(\d+)', rest, re.M)
    return int(ssrc.group(1)) if ssrc else None


_STATS_FIELDS = ('packets-received', 'packets-lost', 'bytes-received', 'jitter', 'packets-sent', 'bytes-sent',
                 'round-trip-time', 'fraction-lost')


def _parse_stats(reply):
    """webrtcbin's get-stats reply as {(type, kind): {field: value}} for the RTP stats we show."""
    found = {}
    if reply is None:
        return found

    def field(field_id, value):
        if isinstance(value, Gst.Structure) and value.has_field('type'):
            stats_type = value.get_value('type')
            name = getattr(stats_type, 'value_nick', str(stats_type))
            kind = value.get_value('kind') if value.has_field('kind') else None
            if name in ('inbound-rtp', 'outbound-rtp', 'remote-inbound-rtp') and kind in ('audio', 'video'):
                found[(name, kind)] = {key: value.get_value(key) for key in _STATS_FIELDS if value.has_field(key)}
        return True

    reply.foreach(field)
    return found


def _chosen_video_codec(sdp):
    """(encoding name, payload type) of the first codec of m=video in an answer, or None."""
    match = re.search(r'^m=video (\d+) [^ ]+ ([^\r\n]*)', sdp, re.M)
    if match is None or match.group(1) == '0':
        return None
    rest = sdp[match.end():]
    next_media = re.search(r'^m=', rest, re.M)
    if next_media is not None:
        rest = rest[:next_media.start()]
    for pt in match.group(2).split():
        rtpmap = re.search(r'^a=rtpmap:%s ([^/\r\n]+)' % re.escape(pt), rest, re.M)
        if rtpmap is not None and rtpmap.group(1).upper() not in ('RTX', 'RED', 'ULPFEC', 'FLEXFEC-03'):
            return rtpmap.group(1).upper(), int(pt)
    return None


CVO_URI = 'urn:3gpp:video-orientation'


def _video_section(sdp):
    """(start, end) of the m=video section of an SDP, or None."""
    match = re.search(r'^m=video [^\r\n]*', sdp, re.M)
    if match is None:
        return None
    following = re.search(r'^m=', sdp[match.end():], re.M)
    return match.start(), (match.end() + following.start()) if following is not None else len(sdp)


def _cvo_id(sdp):
    """The RTP header extension id of the coordination of video orientation (CVO) in m=video, or None."""
    section = _video_section(sdp)
    if section is None:
        return None
    match = re.search(r'^a=extmap:(\d+)(?:/\w+)? %s\s*$' % re.escape(CVO_URI), sdp[section[0]:section[1]], re.M)
    return int(match.group(1)) if match else None


def _with_cvo(sdp, extension_id):
    """The answer with the CVO extension accepted in m=video, so the server keeps sending it."""
    section = _video_section(sdp)
    if section is None or _cvo_id(sdp) is not None:
        return sdp
    start, end = section
    line_end = sdp.index('\n', start) + 1
    newline = '\r\n' if sdp[line_end - 2:line_end] == '\r\n' else '\n'
    return sdp[:line_end] + 'a=extmap:%d %s%s' % (extension_id, CVO_URI, newline) + sdp[line_end:]


def _rtp_extension(data, extension_id):
    """The bytes of an RTP header extension element (one or two byte form, RFC 8285), or None."""
    if len(data) < 12 or not data[0] & 0x10:
        return None
    offset = 12 + 4 * (data[0] & 0x0f)
    if len(data) < offset + 4:
        return None
    profile = (data[offset] << 8) | data[offset + 1]
    end = offset + 4 + 4 * ((data[offset + 2] << 8) | data[offset + 3])
    position = offset + 4
    if profile == 0xbede:
        while position < end and position < len(data):
            byte = data[position]
            if byte == 0:
                position += 1           # padding
                continue
            element_id, length = byte >> 4, (byte & 0x0f) + 1
            if element_id == 15:
                return None
            if element_id == extension_id:
                return data[position + 1:position + 1 + length]
            position += 1 + length
    elif profile & 0xfff0 == 0x1000:
        while position + 1 < end and position + 1 < len(data):
            element_id = data[position]
            if element_id == 0:
                position += 1
                continue
            length = data[position + 1]
            if element_id == extension_id:
                return data[position + 2:position + 2 + length]
            position += 2 + length
    return None


def _mids(sdp):
    """The mid of each m-line, in order."""
    mids = []
    for section in re.split(r'^(?=m=)', sdp, flags=re.M)[1:]:
        match = re.search(r'^a=mid:(\S+)', section, re.M)
        mids.append(match.group(1) if match else None)
    return mids


def _contact_name(uri):
    """The name of the contact (not a passer-by) that has uri, or None."""
    if not uri or '@' not in uri:
        return None
    try:
        from blink.contacts import URIUtils
        contact, contact_uri = URIUtils.find_contact(uri)
    except Exception:
        return None
    if getattr(contact, 'type', 'dummy') == 'dummy':
        return None
    return contact.name or None


SPEAKING_DB = -38              # dBFS peak of decoded audio (GStreamer level) above which a participant speaks
SPEAKING_SIP_LEVEL = 25        # the same for the room's audio levels of SIP participants (the mixer's 0-255 scale)
SPEAKING_HOLD = 0.6            # s speaking lasts after the last loud level, so a tile does not flicker between words
LEVEL_INTERVAL = 150 * 1000000 # ns between GStreamer level messages


class Participant(QObject):
    """Someone in the room: a WebRTC publisher (with a feed we subscribe to) or a SIP caller behind the bridge."""

    frameReady = pyqtSignal(QImage)     # emitted on a streaming thread; connections from the GUI are queued
    changed = pyqtSignal()
    speakingChanged = pyqtSignal(bool)  # from the level of its audio (WebRTC) or the room's audio levels (SIP)

    def __init__(self, publisher):
        super(Participant, self).__init__()
        self.id = publisher.get('id')
        self.uri = (publisher.get('uri') or '').replace('sip:', '', 1)
        self.display_name = publisher.get('display_name') or ''
        self._contact_name = None    # looked up once, on first use
        self.type = publisher.get('type') or 'webrtc'
        self.parent = publisher.get('parent') or None   # an extra video of another participant: that participant's publisher id
        self.label = publisher.get('label') or None     # and its name ("Camera 1")
        self.session = None                             # the room session it is in (set when added), for its parent's name
        self.muted = bool(publisher.get('muted'))
        self.feed = None            # feed id once attached
        self.state = None           # None, attaching, connecting, established, failed
        self.has_video = False
        # whether the feed carries video: None until its offer comes; a SIP caller never has any
        self.video = False if self.type == 'sip' else None
        self.video_bytes = 0        # RTP bytes of video received, for the bitrate that says whether video flows
        self.video_codec = None     # the video codec of the feed (our answer's), for the tile's stats
        self.stats = {}             # (type, kind) -> fields, from its webrtcbin's get-stats (the info panel)
        self.speaking = False
        self._speaking_until = 0.0
        self.webrtcbin = None
        self.elements = []          # what this feed added to the pipeline
        self.mixer_pads = []
        self.is_self = False        # our own SIP leg into the room (the MSRP chat), seen behind the bridge
        self.cvo_id = None          # the extension id of the video orientation the sender may use
        self.rotation = 0           # degrees clockwise the picture is turned to be upright (CVO)

    def __repr__(self):
        return f'<Participant {self.uri} {self.type} {self.state}>'

    @property
    def name(self):
        """The name of the contact with this address in our contacts, else the one the room gives, else the address.
        An extra video: its participant's name and its label, "Adrian · Camera 1"."""
        if self.parent:
            owner = self.session.participants.get(self.parent) if self.session is not None else None
            if owner is not None:
                return f'{owner.name} · {self.label or "Camera"}'
            return self.display_name or f'{self.uri} · {self.label or "Camera"}'
        if self._contact_name is None:
            self._contact_name = _contact_name(self.uri) or ''
        return self._contact_name or self.display_name or self.uri

    @property
    def subscribable(self):
        return self.type != 'sip'

    @property
    def visible(self):
        return self.type != 'bridge' and not self.is_self

    def set_level(self, loud):
        """GUI thread: loud is whether this level counts as speech; speaking holds for SPEAKING_HOLD after the last loud one."""
        now = monotonic()
        if loud:
            self._speaking_until = now + SPEAKING_HOLD
        speaking = now < self._speaking_until
        if speaking != self.speaking:
            self.speaking = speaking
            self.speakingChanged.emit(speaking)

    def update(self, publisher):
        if 'muted' in publisher and publisher.get('muted') is not None and bool(publisher['muted']) != self.muted:
            self.muted = bool(publisher['muted'])
            self.changed.emit()
        if publisher.get('display_name') and publisher['display_name'] != self.display_name:
            self.display_name = publisher['display_name']
            self.changed.emit()


class VideoroomSession(QObject):
    """One room joined from one account."""

    stateChanged = pyqtSignal(str, str)         # state, reason
    participantJoined = pyqtSignal(object)
    participantLeft = pyqtSignal(object)
    participantsChanged = pyqtSignal()
    durationKnown = pyqtSignal(int)             # seconds the room has existed, at join
    audioMutedChanged = pyqtSignal(bool)        # also when a moderator mutes us
    speakersChanged = pyqtSignal(object, str)   # publisher ids of the room's speakers (ours is self.id), who chose them
    raisedHandsChanged = pyqtSignal(object)     # publisher ids with a raised hand (ours is self.id)
    mutedByOther = pyqtSignal(str)              # who muted everyone (our microphone muted with them)
    speakingChanged = pyqtSignal(bool)          # our own microphone: speaking or not (after mute)
    inviteStatus = pyqtSignal(str, str, int, str)   # uri, state (progress, success, failed), SIP code, reason

    def __init__(self, account, room, audio=True, video=True, target=None):
        super(VideoroomSession, self).__init__()
        self.account = account
        self.room = room.replace('sip:', '', 1)
        # a room of another SylkServer (blink.webrtc.RoomTarget): its server, its ICE servers, its guests
        self.target = target if target is not None and target.remote else None
        # the address of the same room's MSRP switch, which the MSRP window has
        self.sip_room = target.sip_uri if target is not None else self.room
        self.identity = str(account.id)     # who we are in the room: ourselves, or a guest of the other server
        self._guest = False
        self.id = str(uuid.uuid4())
        self.state = 'idle'         # idle, connecting, accepted, established, terminated
        self.reason = ''
        self.participants = {}      # publisher id -> Participant
        self.feeds = {}             # feed id -> Participant
        self.speakers = []          # publisher ids the room shows large, for everyone (videoroom-configure); [] is the grid
        self.raised_hands = []      # publisher ids with a raised hand, in the order raised
        self.video_codec = self.video_encoder_name = self.video_pt = None   # what we send (the answer's codec, our encoder, its payload type)
        self.extra_videos = {}      # session id -> ExtraVideo: our extra videos in the room (blink.webrtc.extravideo)
        self.stats = {}             # our publisher's get-stats (type, kind) -> fields (the info panel)
        self.video_paused = False   # the feeds' video stopped at the server (our window is out of sight)
        self._muting_everyone = False   # we asked: the room's mute-audio is not for us
        self.audio_muted = not audio    # joined without audio: the microphone starts muted
        self.video_muted = not video    # joined without video: the camera starts off (black is sent)
        self.connection = None
        self.pipeline = None
        self.publisher = None       # its webrtcbin
        self._mixer = None
        self._volume = None
        self._camera_src = None
        self._camera_caps = None
        self._camera_renderer = None
        self._blurring = False      # the camera comes from blink.webrtc.background_blur
        self._video_pad = None
        self._black_timer = QTimer(self)
        self._black_timer.setInterval(200)
        self._black_timer.timeout.connect(self._push_black_frame)
        self._establish_timer = QTimer(self)
        self._establish_timer.setSingleShot(True)
        self._establish_timer.setInterval(PUBLISHER_TIMEOUT)
        self._establish_timer.timeout.connect(lambda: self._terminate('no media connection'))
        self.ice_servers = []
        # candidates wait until the request that creates their session on the server
        # (videoroom-join, videoroom-feed-answer) has been sent
        self._trickle_ready = set()
        self._trickle_pending = {}
        self._join_sent = False
        self._invitees = []         # to invite once the room is joined

    def __repr__(self):
        return f'<VideoroomSession {self.room} {self.state}>'

    def log(self, text, level='info'):
        getattr(ActivityLog(), level)(f'[videoroom] {self.room}: {text}')

    # Joining and leaving

    def join(self):
        if self.state != 'idle':
            return
        if self.target is not None:
            url = self.target.ws_server
            self.ice_servers = self.target.ice_servers
        else:
            url = ws_server(self.account)
            if url is None:
                self._terminate('the account has no SylkServer WebSocket server')
                return
            from blink.sylk_discovery import SylkServerDiscovery
            try:
                self.ice_servers = (SylkServerDiscovery().configuration(self.account.id.domain) or {}).get('iceServers') or []
            except Exception:
                self.ice_servers = []
        self._set_state('connecting')
        self.log(f'joining from {self.account.id} through {url}' + (f' (the SylkServer of {self.target.domain})' if self.target is not None else ''))
        self.connection = SylkRTCConnections().get(url)
        self.connection.received.connect(self._SH_ConnectionEvent)
        self.connection.closed.connect(self._SH_ConnectionClosed)
        self.connection.add_account(self.account, self._account_added)

    def _join_as_guest(self):
        """Another server that does not take our domain: join as a guest of it (its defaultGuestDomain)."""
        import os
        from hashlib import md5
        name = re.sub(r'[^a-z0-9]+', '', (self.account.display_name or self.account.id.username or 'blink').lower()) or 'blink'
        self._guest = True
        self.identity = '%s%s@%s' % (name, os.urandom(2).hex(), self.target.guest_domain)
        username, _, domain = self.identity.partition('@')
        password = md5(f'{username}:{domain}:{os.urandom(8).hex()}'.encode()).hexdigest()     # never checked: guests do not register
        self.log(f'{self.target.domain} does not take {self.account.id.domain}: joining as the guest {self.identity}')
        self.connection.add_identity(self.identity, password, self.account.display_name or str(self.account.id), self._account_added)

    def _account_added(self, error):
        if self.state != 'connecting':
            return
        if error is not None and self.target is not None and not self._guest and 'not allowed' in str(error).lower():
            self._join_as_guest()
            return
        if error is not None:
            self._terminate(f'the server refused the account: {error}')
            return
        try:
            self._start_media()
        except Exception as e:
            self.log(f'cannot start the media: {e}', 'error')
            self._terminate(f'cannot start the media: {e}')
            return
        self._establish_timer.start()

    def leave(self):
        if self.state in ('idle', 'terminated'):
            return
        self.log('leaving')
        if self.connection is not None and self.connection.state == 'ready' and self._join_sent:
            self.connection.request(dict(sylkrtc='videoroom-leave', session=self.id))
        self._terminate('left')

    def _terminate(self, reason):
        if self.state == 'terminated':
            return
        for extra in list(self.extra_videos.values()):
            extra.stop('left')      # our extra videos go with us (the server ends them too)
        self.reason = reason
        self._establish_timer.stop()
        self._black_timer.stop()
        self._stop_camera()
        self._camera_src = None
        if self.pipeline is not None:
            pipeline, self.pipeline = self.pipeline, None
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_bus().set_sync_handler(None, None)
        self.publisher = None
        if self.connection is not None:
            try:
                self.connection.received.disconnect(self._SH_ConnectionEvent)
                self.connection.closed.disconnect(self._SH_ConnectionClosed)
            except TypeError:
                pass
            connection, self.connection = self.connection, None
            if not any(session.connection is connection for session in VideoroomManager().sessions if session is not self):
                QTimer.singleShot(1000, lambda: SylkRTCConnections().release(connection))
        self.log(f'ended: {reason}')
        self._set_state('terminated', reason)
        VideoroomManager()._session_ended(self)

    def _set_state(self, state, reason=''):
        self.state = state
        self.stateChanged.emit(state, reason)
        # for the MSRP window of the same room: no audio there while the room has ours
        NotificationCenter().post_notification('VideoroomSessionDidChangeState', sender=self, data=NotificationData(state=state, reason=reason, room=self.room))

    # Media: the publisher

    def _start_media(self):
        _init()
        self.pipeline = Gst.Pipeline.new('videoroom')
        # no GLib main loop to watch the bus from: messages are taken as they are posted
        self.pipeline.get_bus().set_sync_handler(self._on_bus_message, None)

        # what is played: every feed's audio, mixed, through the echo probe
        self._mixer = _make('audiomixer', 'mixer', ignore_inactive_pads=True)
        playback = [self._mixer, _make('audioconvert'), _make('audioresample')]
        if _has('webrtcechoprobe'):
            playback.append(_make('webrtcechoprobe', 'echoprobe'))
        playback.append(_make('autoaudiosink', 'speakers'))
        self._add_chain(playback)

        self.publisher = webrtcbin = _make('webrtcbin', 'publisher', bundle_policy='max-bundle')
        self._configure_ice(webrtcbin)
        self.pipeline.add(webrtcbin)
        webrtcbin.connect('on-negotiation-needed', lambda element: None)    # the offer is made below, once
        webrtcbin.connect('on-ice-candidate', self._on_ice_candidate, self.id)
        webrtcbin.connect('notify::ice-gathering-state', self._on_gathering_state, self.id)
        webrtcbin.connect('notify::ice-connection-state', self._on_ice_connection_state, 'publisher')

        # microphone
        microphone = [_make('autoaudiosrc', 'microphone'), _make('audioconvert'), _make('audioresample')]
        if _has('webrtcdsp') and _has('webrtcechoprobe'):
            microphone += [_make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,rate=48000,channels=1')), _make('webrtcdsp', 'dsp', probe='echoprobe')]
        self._volume = _make('volume', 'micvolume', mute=self.audio_muted)
        microphone += [self._volume] + ([_make('level', 'level-self', interval=LEVEL_INTERVAL, post_messages=True)] if _has('level') else []) + [_make('audioconvert'), _make('audioresample'), _make('queue', max_size_time=200 * Gst.MSECOND, leaky='downstream'),
                       _make('opusenc', bitrate=32000, inband_fec=True, audio_type='voice'),
                       _make('rtpopuspay', pt=OPUS_PT)]
        self._add_chain(microphone)
        audio_caps = Gst.Caps.from_string(f'application/x-rtp,media=audio,encoding-name=OPUS,payload={OPUS_PT},clock-rate=48000,encoding-params=(string)2')
        microphone_out = microphone[-1]
        audio_pad = webrtcbin.request_pad_simple('sink_%u')
        audio_transceiver = audio_pad.get_property('transceiver')
        audio_transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        # the offer is made before the microphone has produced caps: it is built from these
        audio_transceiver.set_property('codec-preferences', audio_caps)
        capsfilter = _make('capsfilter', caps=audio_caps)
        self.pipeline.add(capsfilter)
        microphone_out.link(capsfilter)
        capsfilter.get_static_pad('src').link(audio_pad)

        # camera: the pad is linked once the answer names the codec
        self._video_pad = webrtcbin.request_pad_simple('sink_%u')
        transceiver = self._video_pad.get_property('transceiver')
        transceiver.set_property('direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        preferences = Gst.Caps.new_empty()
        offered = set()
        # our video SSRC, chosen now: the encoder chain (its payloader) only exists after the answer,
        # so webrtcbin would offer no a=ssrc for it, and the server drops RTP from an SSRC it was not
        # told (the browsers announce theirs). In the codec preferences webrtcbin offers it.
        import random
        self._video_ssrc = random.randint(1, 0xffffffff)
        for name, pt, encoder, payloader in VIDEO_CODECS:
            if (name, pt) in offered or not (_has(encoder) and _has(payloader)):
                continue
            offered.add((name, pt))
            caps = f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000,rtcp-fb-nack=(boolean)true,rtcp-fb-nack-pli=(boolean)true,rtcp-fb-ccm-fir=(boolean)true,ssrc=(uint){self._video_ssrc}'
            if name == 'H264':
                caps += ',packetization-mode=(string)1,profile-level-id=(string)42e01f,level-asymmetry-allowed=(string)1'
            preferences.append(Gst.Caps.from_string(caps))
        transceiver.set_property('codec-preferences', preferences)
        self.log(f'offering video {", ".join(name for name, pt in sorted(offered, key=lambda item: -item[1]))}')

        self.pipeline.set_state(Gst.State.PLAYING)

        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        webrtcbin.emit('create-offer', None, promise)

    def _add_chain(self, elements):
        for element in elements:
            self.pipeline.add(element)
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeError(f'cannot link {left.get_name()} to {right.get_name()}')

    def _configure_ice(self, webrtcbin):
        stun, turn = _ice_urls(self.ice_servers)
        if stun:
            webrtcbin.set_property('stun-server', stun)
        for url in turn:
            webrtcbin.emit('add-turn-server', url)

    def _on_offer_created(self, promise, data):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        if offer is None:
            call_in_gui_thread(self._terminate, 'cannot create the offer')
            return
        self.publisher.emit('set-local-description', offer, Gst.Promise.new())
        call_in_gui_thread(self._send_join, offer.sdp.as_text())

    def _send_join(self, sdp):
        if self.state != 'connecting':
            return
        self.log(f'sending the offer ({_media_summary(sdp)})')
        self._offer_video_ssrc = _video_ssrc(sdp)
        if self._offer_video_ssrc is None:
            self._offer_video_ssrc = getattr(self, '_video_ssrc', None)     # not in the SDP: the one chosen, at least
        if _video_ssrc(sdp) is None:
            self.log('the offer has no SSRC for our video: the server may drop it', 'warning')
        ActivityLog().debug(f'[videoroom] {self.room}: offer\n{sdp}')
        request = dict(sylkrtc='videoroom-join', account=self.identity, session=self.id, uri=self.room, sdp=sdp, audio=True, video=True)
        if not self._guest:
            # our MSRP chat leg to the room carries this +sip.instance: the gateway then knows it is us
            from sipsimple.configuration.settings import SIPSimpleSettings
            instance_id = SIPSimpleSettings().instance_id
            if instance_id:
                request['sip_instance'] = str(instance_id)
        self.connection.request(request, self._join_answered)
        self._join_sent = True
        self._flush_trickle(self.id)

    def _join_answered(self, error):
        if error is not None:
            self._terminate(f'cannot join: {error}')

    def _on_answer(self, sdp):
        self.log(f'answer: {_media_summary(sdp)}')
        ActivityLog().debug(f'[videoroom] {self.room}: answer\n{sdp}')
        codec = _chosen_video_codec(sdp)
        if codec is not None:
            try:
                self._link_video_encoder(*codec)
            except Exception as e:
                self.log(f'cannot send video: {e}', 'warning')
        else:
            self.log('the room takes no video from us', 'warning')
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, _sdp_message(sdp))
        self.publisher.emit('set-remote-description', answer, Gst.Promise.new())

    def _link_video_encoder(self, name, pt):
        for codec, codec_pt, encoder_factory, payloader_factory in VIDEO_CODECS:
            if codec == name and _has(encoder_factory) and _has(payloader_factory):
                break
        else:
            raise RuntimeError(f'no encoder for {name}')
        if encoder_factory == 'vp9enc':
            encoder = _make('vp9enc', deadline=1, cpu_used=8, row_mt=True, target_bitrate=VIDEO_BITRATE * 1000,
                            keyframe_max_dist=VIDEO_FPS * 4, end_usage='cbr', lag_in_frames=0, error_resilient='default')
        elif encoder_factory == 'vp8enc':
            encoder = _make('vp8enc', deadline=1, cpu_used=8, target_bitrate=VIDEO_BITRATE * 1000,
                            keyframe_max_dist=VIDEO_FPS * 4, end_usage='cbr', lag_in_frames=0, error_resilient='default')
        elif encoder_factory == 'x264enc':
            encoder = _make('x264enc', tune='zerolatency', speed_preset='ultrafast', bitrate=VIDEO_BITRATE, key_int_max=VIDEO_FPS * 4, bframes=0)
        else:
            encoder = _make('openh264enc', bitrate=VIDEO_BITRATE * 1000, gop_size=VIDEO_FPS * 4)
        # the transceiver offered every codec with its fmtp (profile-level-id ...): what the encoder
        # makes would not match those, so it is narrowed to the codec the answer chose
        rtp_caps = f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000'
        if getattr(self, '_offer_video_ssrc', None) is not None:
            rtp_caps += f',ssrc=(uint){self._offer_video_ssrc}'
        self._video_pad.get_property('transceiver').set_property('codec-preferences', Gst.Caps.from_string(rtp_caps))
        # the camera's source exists only from here on: an appsrc that is not linked fails the pipeline
        self._camera_src = _make('appsrc', 'camera', is_live=True, do_timestamp=True, format='time', block=False,
                                 max_bytes=3840 * 2160 * 4, leaky_type='downstream')
        self.pipeline.add(self._camera_src)
        self._video_caps = _make('capsfilter', caps=self._video_caps_for(False))
        self._video_encoder, self._video_encoder_factory = encoder, encoder_factory
        self._sending_screen = False
        chain = [self._camera_src, _make('videoconvert'), _make('videoscale', add_borders=True), _make('videorate', drop_only=True),
                 self._video_caps, _make('queue', max_size_buffers=2, leaky='downstream'), encoder]
        if encoder_factory == 'x264enc':
            chain.append(_make('capsfilter', caps=Gst.Caps.from_string('video/x-h264,profile=constrained-baseline')))
        # the encoder chain is only built now, after the answer: its payloader must send with the SSRC
        # the offer announced (a=ssrc), or the server drops what it does not know
        ssrc = getattr(self, '_offer_video_ssrc', None)
        payloader = _make(payloader_factory, pt=pt, mtu=1200, config_interval=-1, **(dict(ssrc=ssrc) if ssrc is not None else {}))
        chain.append(payloader)
        chain.append(_make('capsfilter', caps=Gst.Caps.from_string(f'application/x-rtp,media=video,encoding-name={name},payload={pt},clock-rate=90000')))
        for element in chain[1:]:
            self.pipeline.add(element)
        for left, right in zip(chain, chain[1:]):
            if not left.link(right):
                raise RuntimeError(f'cannot link {left.get_name()} to {right.get_name()}')
        if chain[-1].get_static_pad('src').link(self._video_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError('cannot link the video to webrtcbin')
        for element in chain:
            element.sync_state_with_parent()
        self.video_codec, self.video_encoder_name, self.video_pt = name, encoder_factory, pt
        self.log(f'sending video {name} with {encoder_factory}')
        self._apply_video_profile()     # sharing the screen already
        self._start_camera()

    # The camera

    def _start_camera(self):
        if self.video_muted:
            self._black_timer.start()
        else:
            self._attach_camera()

    def _attach_camera(self):
        from sipsimple.application import SIPApplication
        from sipsimple.core import FrameBufferVideoRenderer
        try:
            producer = SIPApplication.video_device.producer
        except AttributeError:
            producer = None
        if producer is None:
            self.log('no camera: sending black', 'warning')
            self._black_timer.start()
            return
        from blink.webrtc.background_blur import BlurredCamera, blur_enabled
        if blur_enabled() and not self.sending_screen_device():
            self._blurring = True
            BlurredCamera().add_listener(self, self._on_blurred_frame)
            self.log('sending the camera with its background blurred')
            return
        try:
            self._camera_renderer = FrameBufferVideoRenderer(self._on_camera_frame)
            self._camera_renderer.producer = producer
        except Exception as e:
            self.log(f'cannot read the camera ({e}): sending black', 'warning')
            self._camera_renderer = None
            self._black_timer.start()

    def _stop_camera(self):
        if self._blurring:
            from blink.webrtc.background_blur import BlurredCamera
            self._blurring = False
            BlurredCamera().remove_listener(self)
        renderer, self._camera_renderer = self._camera_renderer, None
        if renderer is not None:
            try:
                renderer.close()
            except Exception:
                pass

    def camera_changed(self):
        """The camera device was switched in Blink (the screen, when it is shared): read the new one,
        and send it at the size that suits it."""
        if self.pipeline is None or self._camera_src is None:
            return
        self._apply_video_profile()
        if self.video_muted:
            return
        self._stop_camera()
        self._attach_camera()

    def blur_changed(self):
        """The background blur was turned on or off: the camera is read again, through it or not."""
        if self.pipeline is None or self._camera_src is None or self.video_muted:
            return
        self._stop_camera()
        self._attach_camera()

    @staticmethod
    def sending_screen_device():
        """Whether Blink's video device is the screen (blink.screen_share)."""
        from sipsimple.application import SIPApplication
        from blink.screen_pointer import is_screen_device
        try:
            return is_screen_device(SIPApplication.video_device.real_name)
        except Exception:
            return False

    @staticmethod
    def _video_caps_for(screen):
        width, height, fps = (SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_FPS) if screen else (VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS)
        return Gst.Caps.from_string(f'video/x-raw,format=I420,width={width},height={height},framerate={fps}/1,pixel-aspect-ratio=1/1')

    def _apply_video_profile(self):
        """The screen goes at 720p, 15 fps and a higher bitrate; the camera at VGA, 24 fps (renegotiated in place)."""
        screen = self.sending_screen_device()
        if getattr(self, '_video_caps', None) is None or screen == getattr(self, '_sending_screen', False):
            return
        self._sending_screen = screen
        if SCREEN_CHANGES_SIZE:
            self._video_caps.set_property('caps', self._video_caps_for(screen))
        bitrate = SCREEN_BITRATE if screen else VIDEO_BITRATE
        encoder = self._video_encoder
        if self._video_encoder_factory in ('vp8enc', 'vp9enc'):
            encoder.set_property('target-bitrate', bitrate * 1000)
        elif self._video_encoder_factory == 'x264enc':
            encoder.set_property('bitrate', bitrate)
        elif encoder.find_property('bitrate') is not None:
            encoder.set_property('bitrate', bitrate * 1000)
        size = (SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_FPS) if screen and SCREEN_CHANGES_SIZE else (VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS)
        self.log(f'sending the {"screen" if screen else "camera"} at %dx%d, %d fps, {bitrate} kbit/s' % size)

    def _on_camera_frame(self, frame):
        # pjsip's video thread: hand the frame to GStreamer and return
        source = self._camera_src
        if source is None or self.video_muted:
            return
        self._push_frame(bytes(frame.data), frame.width, frame.height)

    def _on_blurred_frame(self, data, width, height):
        # the blur thread
        if self._camera_src is None or self.video_muted or not self._blurring:
            return
        self._push_frame(data, width, height)

    def _push_frame(self, data, width, height):
        # frames come in the producer's format: BGRA from the cameras, I420 from the screen device
        source = self._camera_src
        if source is None:
            return
        if len(data) >= width * height * 4:
            video_format, size = 'BGRA', width * height * 4
        elif len(data) >= width * height * 3 // 2 and width % 2 == 0 and height % 2 == 0:
            video_format, size = 'I420', width * height * 3 // 2
        else:
            if not getattr(self, '_odd_frame_logged', False):
                self._odd_frame_logged = True
                call_in_gui_thread(self.log, f'camera frames of {len(data)} bytes for {width}x{height} are neither BGRA nor I420: not sent', 'warning')
            return
        if self._camera_caps != (width, height, video_format):
            self._camera_caps = (width, height, video_format)
            source.set_property('caps', Gst.Caps.from_string(f'video/x-raw,format={video_format},width={width},height={height},framerate=0/1'))
        source.emit('push-buffer', Gst.Buffer.new_wrapped(data[:size]))

    def _push_black_frame(self):
        width, height = self._camera_caps[:2] if self._camera_caps else (VIDEO_WIDTH, VIDEO_HEIGHT)
        if getattr(self, '_black', None) is None or len(self._black) != width * height * 4:
            self._black = bytes([0, 0, 0, 255]) * (width * height)
        self._push_frame(self._black, width, height)

    # Controls

    def set_speakers(self, ids):
        """Make these publishers (ours is self.id; none, one or two) the room's speakers: shown large
        to everyone, as Sylk desktop's and Sylk Mobile's speaker selection does."""
        ids = [str(id) for id in ids][:2]
        if self.connection is None or self.state != 'established':
            return
        names = [self.speaker_name(id) for id in ids]
        self.log(f'setting the speakers to {", ".join(names) or "nobody (grid)"}')

        def configured(error):
            if error is not None:
                self.log(f'setting the speakers failed: {error}', 'warning')

        self.connection.request(dict(sylkrtc='videoroom-configure', session=self.id, active_participants=ids), configured)

    # Moderation, as Sylk Mobile does it (react-native-sylkrtc Conference)

    def toggle_hand(self, session_id=None):
        """Raise or lower our hand; with session_id, lower someone else's."""
        if self.connection is None or self.state != 'established':
            return
        request = dict(sylkrtc='videoroom-toggle-hand', session=self.id)
        if session_id is not None:
            request['session_id'] = session_id
        self.log('lowering the hand of %s' % self.speaker_name(session_id) if session_id else
                 ('lowering our hand' if self.id in self.raised_hands else 'raising our hand'))
        self.connection.request(request)

    def mute_everyone(self):
        """Ask everyone in the room to mute their microphone (not us)."""
        if self.connection is None or self.state != 'established':
            return
        self.log('muting everyone')
        self._muting_everyone = True
        QTimer.singleShot(3000, lambda: setattr(self, '_muting_everyone', False))
        self.connection.request(dict(sylkrtc='videoroom-mute-audio-participants', session=self.id))

    def mute_participant(self, participant, muted=True):
        """Mute or unmute one participant: a WebRTC one is asked (mute-request), a SIP one is muted at the mix."""
        if self.connection is None or self.state != 'established':
            return
        self.log(f'{"muting" if muted else "unmuting"} {participant.uri}')
        self.connection.request(dict(sylkrtc='videoroom-mute-participant', session=self.id, participant_id=participant.id, muted=bool(muted)))

    def remove_participants(self, uris):
        """Remove SIP participants from the room (the server asks the focus to end their sessions)."""
        uris = [uri if uri.startswith('sip:') else 'sip:' + uri for uri in uris]
        if not uris or self.connection is None or self.state != 'established':
            return
        self.log(f'removing {", ".join(uris)}')
        self.connection.request(dict(sylkrtc='videoroom-remove', session=self.id, participants=uris))

    def speaker_name(self, id):
        if id == self.id:
            return 'you'
        extra = self.extra_videos.get(id)
        if extra is not None:
            return f'your {extra.label}'
        participant = self.participants.get(id)
        return participant.name if participant is not None else id

    def invite(self, uris):
        """Invite people to the room (the server calls them)."""
        uris = [uri.replace('sip:', '', 1) for uri in uris]
        if self.state != 'established':
            self._invitees.extend(uris)
            return
        self.log(f'inviting {", ".join(uris)}')
        self.connection.request(dict(sylkrtc='videoroom-invite', session=self.id, participants=uris))

    # Statistics, for the info panel

    def request_stats(self):
        """Ask every webrtcbin (ours and the feeds') for its statistics; they land in .stats when ready."""
        if self.publisher is not None:
            self._get_stats(self.publisher, lambda stats: setattr(self, 'stats', stats))
        for participant in list(self.feeds.values()):
            if participant.webrtcbin is not None:
                self._get_stats(participant.webrtcbin, lambda stats, participant=participant: setattr(participant, 'stats', stats))
        for extra in list(self.extra_videos.values()):      # our other videos: what each sends
            if extra.webrtcbin is not None:
                self._get_stats(extra.webrtcbin, lambda stats, participant=extra.participant: setattr(participant, 'stats', stats))

    @staticmethod
    def _get_stats(webrtcbin, store):
        def done(promise, data):
            # streaming thread
            promise.wait()
            call_in_gui_thread(store, _parse_stats(promise.get_reply()))
        try:
            webrtcbin.emit('get-stats', None, Gst.Promise.new_with_change_func(done, None))
        except Exception:
            pass

    def set_video_paused(self, paused):
        """Stop (or resume) the video of every feed at the server, audio going on, as Sylk Mobile's
        pauseVideo/resumeVideo: nobody's video is sent to us while nobody can see it."""
        paused = bool(paused)
        if paused == self.video_paused:
            return
        self.video_paused = paused
        self.log('pausing the video of the feeds: the window is out of sight' if paused else 'resuming the video of the feeds')
        for participant in list(self.feeds.values()):
            self._update_feed_video(participant)

    def _update_feed_video(self, participant):
        if self.connection is None or participant.feed not in self.feeds or participant.state not in ('connecting', 'established'):
            return

        def updated(error):
            if error is not None:
                self.log(f'{"pausing" if self.video_paused else "resuming"} the video of {participant.uri} failed: {error}', 'warning')

        self.connection.request(dict(sylkrtc='videoroom-session-update', session=participant.feed, options=dict(video=not self.video_paused)), updated)

    def refresh_video(self, participant):
        """Pause and resume a feed's video, as Sylk Mobile does on a stall: Janus then sends it a new keyframe."""
        if self.connection is None or participant.feed not in self.feeds:
            return
        self.log(f'{participant.uri} video stalled, asking for it again')

        def resume():
            if self.connection is not None and participant.feed in self.feeds:
                self.connection.request(dict(sylkrtc='videoroom-session-update', session=participant.feed, options=dict(video=True)))

        self.connection.request(dict(sylkrtc='videoroom-session-update', session=participant.feed, options=dict(video=False)))
        QTimer.singleShot(300, resume)

    def set_audio_muted(self, muted):
        self.audio_muted = bool(muted)
        if self._volume is not None:
            self._volume.set_property('mute', self.audio_muted)
        self.log('microphone muted' if self.audio_muted else 'microphone unmuted')
        self.audioMutedChanged.emit(self.audio_muted)
        NotificationCenter().post_notification('VideoroomSessionAudioMutedDidChange', sender=self, data=NotificationData(muted=self.audio_muted, room=self.room))

    def set_video_muted(self, muted):
        self.video_muted = bool(muted)
        if self.pipeline is None or self._camera_src is None:
            return
        if self.video_muted:
            self._stop_camera()
            self._black_timer.start()
        else:
            self._black_timer.stop()
            self._attach_camera()
        self.log('camera off' if self.video_muted else 'camera on')

    # ICE

    def _on_ice_candidate(self, webrtcbin, mline, candidate, session_id):
        call_in_gui_thread(self._send_candidate, webrtcbin, session_id, mline, candidate)

    def _send_candidate(self, webrtcbin, session_id, mline, candidate):
        if self.connection is None or self.state == 'terminated':
            return
        description = webrtcbin.get_property('local-description')
        mids = _mids(description.sdp.as_text()) if description is not None else []
        mid = mids[mline] if mline < len(mids) and mids[mline] is not None else str(mline)
        if candidate.startswith('a='):
            candidate = candidate[2:]
        self._trickle(session_id, [dict(candidate=candidate, sdpMLineIndex=mline, sdpMid=mid)])

    def _trickle(self, session_id, candidates):
        if session_id not in self._trickle_ready:
            self._trickle_pending.setdefault(session_id, []).append(candidates)
            return
        self.connection.request(dict(sylkrtc='videoroom-session-trickle', session=session_id, candidates=candidates))

    def _flush_trickle(self, session_id):
        self._trickle_ready.add(session_id)
        for candidates in self._trickle_pending.pop(session_id, []):
            self._trickle(session_id, candidates)

    def _on_gathering_state(self, webrtcbin, spec, session_id):
        if webrtcbin.get_property('ice-gathering-state') == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            call_in_gui_thread(self._send_end_of_candidates, session_id)

    def _send_end_of_candidates(self, session_id):
        if self.connection is not None and self.state != 'terminated':
            self._trickle(session_id, [])

    def _on_ice_connection_state(self, webrtcbin, spec, label):
        state = webrtcbin.get_property('ice-connection-state')
        call_in_gui_thread(self.log, f'ICE {label}: {state.value_nick}', 'debug')
        if state == GstWebRTC.WebRTCICEConnectionState.FAILED:
            call_in_gui_thread(self._ice_failed, label)

    def _ice_failed(self, label):
        if label == 'publisher':
            self._terminate('the media connection failed (ICE)')
        else:
            participant = self.feeds.get(label)
            if participant is not None:
                participant.state = 'failed'
                participant.changed.emit()
                self.log(f'media from {participant.uri} failed (ICE)', 'warning')

    # Bus

    def _on_bus_message(self, bus, message, data):
        # any thread
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            call_in_gui_thread(self._bus_error, message.src.get_name() if message.src is not None else '?', error.message, debug)
        elif message.type == Gst.MessageType.ELEMENT and message.src is not None and message.src.get_name().startswith('level-'):
            structure = message.get_structure()
            if structure is not None and structure.get_name() == 'level':
                try:
                    peaks = structure.get_value('peak')
                    peak = max(peaks) if peaks else -100.0
                except (TypeError, ValueError):
                    peak = -100.0
                call_in_gui_thread(self._level, message.src.get_name()[6:], peak > SPEAKING_DB)
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            call_in_gui_thread(self.log, f'GStreamer warning in {message.src.get_name() if message.src else "?"}: {warning.message}', 'debug')
        return Gst.BusSyncReply.DROP

    def _level(self, feed, loud):
        if feed == 'self':
            loud = loud and not self.audio_muted
            now = monotonic()
            if loud:
                self._speaking_until = now + SPEAKING_HOLD
            speaking = now < getattr(self, '_speaking_until', 0)
            if speaking != getattr(self, 'speaking', False):
                self.speaking = speaking
                self.speakingChanged.emit(speaking)
            return
        participant = self.feeds.get(feed)
        if participant is not None:
            participant.set_level(loud)

    def _EH_conference_audio_levels(self, message):
        # SIP participants: the mixer's levels, rx being what comes from them (their speech)
        levels = message.get('levels') or []
        if isinstance(levels, dict):
            levels = [dict(entry, participant_id=pid) for pid, entry in levels.items()]
        for entry in levels:
            participant = self.participants.get(str(entry.get('participant_id') or ''))
            if participant is None:
                continue
            level = entry.get('rx_peak') if entry.get('rx_peak') is not None else entry.get('rx', 0)
            try:
                participant.set_level(float(level) > SPEAKING_SIP_LEVEL)
            except (TypeError, ValueError):
                pass

    def _bus_error(self, source, text, debug):
        if self.state == 'terminated':
            return
        self.log(f'GStreamer error in {source}: {text} ({debug})', 'error')
        # the parts without which there is no call; a feed that breaks only loses that participant
        if source.startswith(('publisher', 'microphone', 'speakers', 'mixer', 'camera')):
            self._terminate(f'media error: {text}')

    # Signalling events

    def _SH_ConnectionClosed(self, reason):
        if self.state != 'terminated':
            self._terminate(f'connection to the server lost: {reason}')

    def _SH_ConnectionEvent(self, message):
        if message.get('sylkrtc') != 'videoroom-event' or message.get('session') != self.id:
            return
        event = message.get('event')
        handler = getattr(self, '_EH_' + (event or '').replace('-', '_'), None)
        if handler is not None:
            try:
                handler(message)
            except Exception as e:
                self.log(f'handling {event} failed: {e}', 'error')
                ActivityLog().debug(f'[videoroom] {message}')

    def _EH_session_state(self, message):
        state = message.get('state')
        if state == 'progress':
            self.log('the server is joining the room')
        elif state == 'accepted':
            if isinstance(message.get('duration'), int):
                self.durationKnown.emit(message['duration'])
            self._set_state('accepted')
            self._on_answer(message.get('sdp') or '')
        elif state == 'established':
            self._establish_timer.stop()
            self.log('media established')
            self._set_state('established')
            invitees, self._invitees = self._invitees, []
            if invitees:
                self.invite(invitees)
        elif state == 'terminated':
            self._terminate(message.get('reason') or 'ended by the server')

    def _EH_initial_publishers(self, message):
        for publisher in message.get('publishers') or []:
            self._add_publisher(publisher)
        self.participantsChanged.emit()

    def _EH_publishers_joined(self, message):
        for publisher in message.get('publishers') or []:
            self._add_publisher(publisher)
        self.participantsChanged.emit()

    def _EH_publishers_left(self, message):
        for publisher_id in message.get('publishers') or []:
            participant = self.participants.pop(publisher_id, None)
            if participant is None:
                continue
            self.log(f'{participant.uri} left')
            self._remove_feed(participant)
            self.participantLeft.emit(participant)
        self.participantsChanged.emit()

    def _EH_configure(self, message):
        ids = [str(id) for id in message.get('active_participants') or []]
        originator = message.get('originator')
        if originator == 'videoroom':
            who = 'the room'
        elif originator:
            who = self.speaker_name(str(originator))
        else:
            who = ''
        self.speakers = ids
        self.log(f'speakers set to {", ".join(self.speaker_name(id) for id in ids) or "nobody (grid)"}' + (f' by {who}' if who else ''))
        self.speakersChanged.emit(list(ids), who)

    def _EH_invite_status(self, message):
        # the outcome of the REFER the server sent the focus for an invite of ours
        uri = str(message.get('participant') or '').replace('sip:', '', 1)
        state = str(message.get('state') or '')
        try:
            code = int(message.get('code') or 0)
        except (TypeError, ValueError):
            code = 0
        reason = str(message.get('reason') or '')
        self.log(f'invite of {uri}: {state}' + (f' ({code} {reason})'.rstrip() if code else ''), 'warning' if state == 'failed' else 'info')
        self.inviteStatus.emit(uri, state, code, reason)

    def _EH_raised_hands(self, message):
        ids = [str(id) for id in message.get('raised_hands') or []]
        if ids != self.raised_hands:
            raised = [id for id in ids if id not in self.raised_hands]
            self.raised_hands = ids
            for id in raised:
                self.log(f'{self.speaker_name(id)} raised {"our" if id == self.id else "a"} hand')
            self.raisedHandsChanged.emit(list(ids))

    def _EH_mute_audio(self, message):
        originator = str(message.get('originator') or '')
        who = self.speaker_name(originator) if originator else 'someone'
        if self._muting_everyone or originator == self.id:
            self._muting_everyone = False
            return      # our own mute everyone
        self.log(f'{who} muted everyone')
        if not self.audio_muted:
            self.set_audio_muted(True)
            self.mutedByOther.emit(who)

    def _EH_publisher_updated(self, message):
        participant = self.participants.get(message.get('publisher'))
        if participant is not None:
            participant.update(dict(muted=message.get('muted')))

    def _EH_feed_attached(self, message):
        participant = self.feeds.get(message.get('feed'))
        if participant is None:
            return
        try:
            self._answer_feed(participant, message.get('sdp') or '')
        except Exception as e:
            self.log(f'cannot receive {participant.uri}: {e}', 'error')
            participant.state = 'failed'
            participant.changed.emit()

    def _EH_feed_established(self, message):
        participant = self.feeds.get(message.get('feed'))
        if participant is not None:
            participant.state = 'established'
            participant.changed.emit()
            self.log(f'receiving {participant.uri}')

    # The room's chat over the WebRTC connection: the gateway relays it to the room's MSRP switch.
    # The MSRP window uses it when its own MSRP session to the room cannot connect (blink.chatwindow).

    def send_chat_message(self, content, content_type='text/plain'):
        """Send a message to the room's chat; its id (the delivery report comes with it)."""
        message_id = uuid.uuid4().hex
        if self.connection is None or self.state == 'terminated':
            raise RuntimeError('not in the room')
        if isinstance(content, bytes):
            content = content.decode('latin1')      # binary goes as latin1 over JSON, the gateway turns it back
        self.connection.request(dict(sylkrtc='videoroom-message', session=self.id, message_id=message_id, content=content, content_type=content_type))
        return message_id

    def _EH_message(self, message):
        sender = message.get('sender') or {}
        NotificationCenter().post_notification('VideoroomSessionGotMessage', sender=self, data=NotificationData(
            type=message.get('type') or 'normal', content=message.get('content') or '', content_type=message.get('content_type') or 'text/plain',
            sender_uri=(sender.get('uri') or '').replace('sip:', '', 1), sender_name=sender.get('display_name') or '', timestamp=message.get('timestamp')))

    def _EH_message_delivery(self, message):
        NotificationCenter().post_notification('VideoroomSessionDidDeliverMessage', sender=self, data=NotificationData(
            message_id=message.get('message_id'), delivered=bool(message.get('delivered')), code=message.get('code'), reason=message.get('reason')))

    def _EH_mute_request(self, message):
        self.log('muted by a moderator' if message.get('muted') else 'unmuted by a moderator')
        self.set_audio_muted(bool(message.get('muted')))

    def _add_publisher(self, publisher):
        publisher_id = publisher.get('id')
        if not publisher_id:
            return
        participant = self.participants.get(publisher_id)
        if participant is not None:
            participant.update(publisher)
            return
        if publisher_id == self.id or publisher_id in self.extra_videos or publisher.get('parent') == self.id:
            return      # ourselves, or one of our extra videos (shown from its own pipeline)
        participant = Participant(publisher)
        participant.session = self
        participant.is_self = participant.type == 'sip' and participant.uri.lower() in (str(self.account.id).lower(), self.identity.lower())
        self.participants[publisher_id] = participant
        self.log(f'{participant.uri} is in the room ({participant.type})')
        self.participantJoined.emit(participant)
        if participant.subscribable:
            self._attach_feed(participant)

    # Feeds: the other publishers

    def _attach_feed(self, participant):
        participant.feed = str(uuid.uuid4())
        participant.state = 'attaching'
        self.feeds[participant.feed] = participant

        def attached(error):
            if error is not None:
                participant.state = 'failed'
                participant.changed.emit()

        self.connection.request(dict(sylkrtc='videoroom-feed-attach', session=self.id, publisher=participant.id, feed=participant.feed), attached)

    def _answer_feed(self, participant, sdp):
        participant.state = 'connecting'
        participant.video = _offers_video(sdp)
        participant.changed.emit()
        self.log(f'offer from {participant.uri}: {_media_summary(sdp)}')
        ActivityLog().debug(f'[videoroom] {self.room}: offer from {participant.uri}\n{sdp}')
        # a mobile sends the camera as it is held and says how it is turned (CVO): the picture is turned here
        participant.cvo_id = _cvo_id(sdp)
        webrtcbin = _make('webrtcbin', 'feed-' + participant.feed, bundle_policy='max-bundle')
        self._configure_ice(webrtcbin)
        participant.webrtcbin = webrtcbin
        participant.elements.append(webrtcbin)
        self.pipeline.add(webrtcbin)
        webrtcbin.connect('pad-added', self._on_feed_pad, participant)
        webrtcbin.connect('on-ice-candidate', self._on_ice_candidate, participant.feed)
        webrtcbin.connect('notify::ice-gathering-state', self._on_gathering_state, participant.feed)
        webrtcbin.connect('notify::ice-connection-state', self._on_ice_connection_state, participant.feed)
        webrtcbin.sync_state_with_parent()

        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, _sdp_message(sdp))

        def remote_set(promise, data):
            promise.wait()
            webrtcbin.emit('create-answer', None, Gst.Promise.new_with_change_func(answer_created, None))

        def answer_created(promise, data):
            promise.wait()
            reply = promise.get_reply()
            answer = reply.get_value('answer') if reply is not None else None
            if answer is None:
                call_in_gui_thread(self.log, f'cannot answer {participant.uri}', 'error')
                return
            webrtcbin.emit('set-local-description', answer, Gst.Promise.new())
            text = answer.sdp.as_text()
            if participant.cvo_id is not None:
                text = _with_cvo(text, participant.cvo_id)      # webrtcbin does not answer extensions it does not implement
            call_in_gui_thread(self._send_feed_answer, participant, text)

        webrtcbin.emit('set-remote-description', offer, Gst.Promise.new_with_change_func(remote_set, None))

    def _send_feed_answer(self, participant, sdp):
        if self.connection is None or participant.feed not in self.feeds:
            return
        codec = _chosen_video_codec(sdp)
        participant.video_codec = codec[0] if codec else None
        self.connection.request(dict(sylkrtc='videoroom-feed-answer', session=self.id, feed=participant.feed, sdp=sdp))
        self._flush_trickle(participant.feed)
        if self.video_paused:
            self._update_feed_video(participant)    # attached while out of sight: paused from the start

    def _on_feed_pad(self, webrtcbin, pad, participant):
        # streaming thread
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        media = caps.get_structure(0).get_string('media') if caps is not None and caps.get_size() else None
        if media == 'video':
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_video_rtp, participant)
        decodebin = _make('decodebin')
        decodebin.connect('pad-added', self._on_decoded_pad, participant)
        decodebin.connect('deep-element-added', self._on_feed_element_added)
        self.pipeline.add(decodebin)
        participant.elements.append(decodebin)
        decodebin.sync_state_with_parent()
        pad.link(decodebin.get_static_pad('sink'))

    @staticmethod
    def _on_feed_element_added(bin, sub_bin, element):
        # streaming thread: the depayloaders decodebin plugs would handle the RTP header extensions
        # themselves (auto-header-extension); after a feed's video is paused and resumed one can drop
        # a frame while holding its extension buffers and GStreamer aborts the process
        # (gstrtpbasedepayload.c: hdrext_buffers == 0). None is needed: the video orientation is
        # read before the depayloader (_on_video_rtp).
        if element.find_property('auto-header-extension') is not None:
            element.set_property('auto-header-extension', False)

    def _on_video_rtp(self, pad, info, participant):
        # streaming thread: the bytes received, and the video orientation from the RTP header extension
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        participant.video_bytes += buffer.get_size()
        if participant.cvo_id is None:
            return Gst.PadProbeReturn.OK
        ok, mapping = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            element = _rtp_extension(mapping.data, participant.cvo_id)
        finally:
            buffer.unmap(mapping)
        if element:
            rotation = (element[0] & 0x03) * 90
            if rotation != participant.rotation:
                participant.rotation = rotation
                call_in_gui_thread(self.log, f'{participant.uri} video is turned {rotation} degrees', 'debug')
        return Gst.PadProbeReturn.OK

    def _on_decoded_pad(self, decodebin, pad, participant):
        # streaming thread
        caps = pad.get_current_caps() or pad.query_caps(None)
        kind = caps.get_structure(0).get_name() if caps is not None and caps.get_size() else ''
        if kind.startswith('video/'):
            sink = _make('appsink', emit_signals=True, max_buffers=1, drop=True, sync=False,
                         caps=Gst.Caps.from_string('video/x-raw,format=BGRx'))
            sink.connect('new-sample', self._on_video_sample, participant)
            chain = [_make('queue', max_size_buffers=2, leaky='downstream'), _make('videoconvert'), sink]
            for element in chain:
                self.pipeline.add(element)
                participant.elements.append(element)
            for left, right in zip(chain, chain[1:]):
                left.link(right)
            for element in chain:
                element.sync_state_with_parent()
            pad.link(chain[0].get_static_pad('sink'))
            participant.has_video = True
        elif kind.startswith('audio/'):
            chain = [_make('queue', max_size_time=200 * Gst.MSECOND, leaky='downstream'), _make('audioconvert'), _make('audioresample'),
                     _make('capsfilter', caps=Gst.Caps.from_string('audio/x-raw,rate=48000,channels=2'))]
            if _has('level'):
                chain.append(_make('level', 'level-' + participant.feed, interval=LEVEL_INTERVAL, post_messages=True))     # who speaks
            for element in chain:
                self.pipeline.add(element)
                participant.elements.append(element)
            for left, right in zip(chain, chain[1:]):
                left.link(right)
            for element in chain:
                element.sync_state_with_parent()
            pad.link(chain[0].get_static_pad('sink'))
            mixer_pad = self._mixer.request_pad_simple('sink_%u')
            chain[-1].get_static_pad('src').link(mixer_pad)
            participant.mixer_pads.append(mixer_pad)
        else:
            sink = _make('fakesink')
            self.pipeline.add(sink)
            participant.elements.append(sink)
            sink.sync_state_with_parent()
            pad.link(sink.get_static_pad('sink'))

    def _on_video_sample(self, sink, participant):
        # streaming thread
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK
        structure = sample.get_caps().get_structure(0)
        width, height = structure.get_value('width'), structure.get_value('height')
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                stride = len(info.data) // height if height else width * 4
                image = QImage(bytes(info.data), width, height, stride, QImage.Format.Format_RGB32).copy()
            finally:
                buffer.unmap(info)
            if participant.rotation:
                image = image.transformed(QTransform().rotate(participant.rotation))
            participant.frameReady.emit(image)
        return Gst.FlowReturn.OK

    def _remove_feed(self, participant):
        feed = participant.feed
        if feed is None:
            return
        self.feeds.pop(feed, None)
        self._trickle_ready.discard(feed)
        self._trickle_pending.pop(feed, None)
        if self.connection is not None and self.state != 'terminated':
            self.connection.request(dict(sylkrtc='videoroom-feed-detach', session=self.id, feed=feed))
        if self.pipeline is None:
            return
        for element in reversed(participant.elements):
            element.set_state(Gst.State.NULL)
            self.pipeline.remove(element)
        for pad in participant.mixer_pads:
            self._mixer.release_request_pad(pad)
        participant.elements = []
        participant.mixer_pads = []
        participant.webrtcbin = None
        participant.feed = None


class VideoroomManager(object, metaclass=Singleton):
    """The rooms joined over WebRTC."""

    def __init__(self):
        self.sessions = []

    def find(self, room):
        """The session in room: its video room address or its MSRP switch address (another SylkServer's differ)."""
        room = room.replace('sip:', '', 1).lower()
        return next((session for session in self.sessions if room in (session.room.lower(), session.sip_room.lower()) and session.state != 'terminated'), None)

    def join(self, account, room, audio=True, video=True, target=None, extra_sources=None, screen=None):
        """Join room from account, or bring forward the session already in it. extra_sources:
        [(source, label)] published as our extra videos once the room is joined (blink.webrtc.extravideo);
        screen: a screen device shared once joined."""
        session = self.find(room)
        if session is not None:
            from blink.webrtc.videoroomwindow import VideoroomWindow
            VideoroomWindow.for_session(session).raise_and_activate()
            return session
        session = VideoroomSession(account, room, audio=audio, video=video, target=target)
        session.pending_extra_sources = list(extra_sources or [])
        session.pending_screen = screen
        self.sessions.append(session)
        from blink.webrtc.videoroomwindow import VideoroomWindow
        VideoroomWindow.for_session(session).show()
        session.join()
        return session

    def leave_all(self):
        for session in list(self.sessions):
            session.leave()

    def camera_changed(self):
        for session in self.sessions:
            session.camera_changed()

    def _session_ended(self, session):
        if session in self.sessions:
            self.sessions.remove(session)
