"""WebRTC conferencing with SylkServer (the webrtcgateway's video rooms).

What is in this package needs GStreamer WebRTC and Qt WebSockets, and is useful only
with a SylkServer domain. Nothing of it is loaded until an account's domain publishes
a wsServer in its SylkServer configuration (blink.sylk_discovery): the checks here
are made first, the submodules imported only after.
"""

import re
import sys

from urllib.parse import unquote, urlsplit

from sipsimple.account import AccountManager, BonjourAccount


__all__ = ['ws_server', 'conference_video_available', 'media_available', 'join_video_conference', 'active_videoroom', 'raise_videoroom_window',
           'drop_sip_audio', 'room_chat_session', 'recent_rooms', 'remember_room', 'parse_room', 'local_room_host', 'RoomTarget', 'room_target', 'room_video', 'resolve_room']


RECENT_ROOMS_KEY = 'conference_dialog/recent_rooms'
MAX_RECENT_ROOMS = 20


def ws_server(account):
    """The wsServer of the account's domain (SylkServer discovery), or None."""
    if account is None or account is BonjourAccount():
        return None
    from blink.sylk_discovery import SylkServerDiscovery
    try:
        configuration = SylkServerDiscovery().configuration(account.id.domain) or {}
    except Exception:
        return None
    server = configuration.get('wsServer')
    if not isinstance(server, str) or not server.strip().startswith(('wss://', 'ws://')):
        return None
    return server.strip()


_media_available = None


def media_available():
    """Whether the WebRTC media can be done here (loads the WebRTC code; checked once a run)."""
    global _media_available
    if _media_available is None:
        from blink.webrtc.videoroom import media_missing
        _media_available = not media_missing()
    return _media_available


def conference_video_available(account=None):
    """Whether account (by default the default account) can join video rooms: its domain runs
    SylkServer with a wsServer and the media elements are installed."""
    if account is None:
        account = AccountManager().default_account
    if ws_server(account) is None:
        return False
    from blink.webrtc.videoroom import available
    return available(account)


def join_video_conference(account, room, audio=True, video=True, target=None):
    """Join room (user@host) over WebRTC from account, the microphone muted without audio and the
    camera off without video (both can be turned on in the room); None when that cannot be done.
    target (a RoomTarget) is a room of another SylkServer: joined through its server."""
    if target is not None and target.remote:
        if not media_available():
            return None
    elif not conference_video_available(account):
        return None
    from blink.webrtc.videoroom import VideoroomManager
    video_room = VideoroomManager().join(account, room, audio=audio, video=video, target=target)
    if video_room is not None:
        drop_sip_audio(video_room)
    return video_room


def _room_sessions(video_room):
    """The Blink sessions (SIP) to the room of video_room, in any of its address forms, not ending."""
    from blink.sessions import SessionManager
    for session in list(SessionManager().sessions):
        contact_uri = getattr(session, 'contact_uri', None)
        state = str(session.state or '')
        if contact_uri is None or state in ('ending', 'ended', 'deleted') or state.startswith('end'):
            continue
        if active_videoroom(contact_uri.uri) is video_room:
            yield session


def drop_sip_audio(video_room):
    """The room's audio and video go over WebRTC now: SIP audio (and video) to the same room would loop
    through it, so it is removed from the sessions that have it; a session with nothing else is ended."""
    from blink.logging import ActivityLog
    for session in _room_sessions(video_room):
        media = [stream for stream in session.streams if stream.type in ('audio', 'video')]
        if not media:
            continue
        state = str(session.state or '')
        # the audio goes on in the video room: ending it here is not a hangup, so no hangup tone (blink.sessions)
        if len(media) == len(session.streams.types):
            ActivityLog().info(f'[conference] {session.contact_uri.uri}: hanging up the SIP audio, the room has it over WebRTC now')
            session.silent_media_end = True
            session.end()
        elif state == 'connected':
            ActivityLog().info(f'[conference] {session.contact_uri.uri}: removing SIP {" and ".join(stream.type for stream in media)}, the room has it over WebRTC now')
            session.silent_media_end = True
            session.remove_streams(media)
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(10000, lambda: setattr(session, 'silent_media_end', False))   # later removals do sound
        else:
            ActivityLog().warning(f'[conference] {session.contact_uri.uri}: SIP {" and ".join(stream.type for stream in media)} kept, the session is {state}')


def room_chat_session(video_room):
    """A session to the room of video_room that has (or is getting) its chat, or None."""
    return next((session for session in _room_sessions(video_room) if 'chat' in session.streams.types), None)


def active_videoroom(uri):
    """The video room joined to uri (a room address in any form), or None; never loads the WebRTC code."""
    module = sys.modules.get('blink.webrtc.videoroom')
    if module is None or uri is None:
        return None
    from blink.conference import split_uri
    user, host = split_uri(uri)
    if not host:
        return None
    return module.VideoroomManager().find(f'{user}@{host}')


def raise_videoroom_window(session):
    """Bring the window of a video room in front (of the MSRP window opened with it)."""
    module = sys.modules.get('blink.webrtc.videoroomwindow')
    window = module.VideoroomWindow.windows.get(session) if module is not None else None
    if window is not None:
        window.raise_and_activate()


def recent_rooms():
    """The rooms joined before from Join Conference, as typed, most recent first."""
    from blink.resources import ui_settings
    value = ui_settings().value(RECENT_ROOMS_KEY, [])
    if isinstance(value, str):
        value = [value] if value else []
    return [str(room) for room in value or [] if room]


def remember_room(room):
    from blink.resources import ui_settings
    room = room.strip()
    if not room:
        return
    rooms = [room] + [other for other in recent_rooms() if other.lower() != room.lower()]
    settings = ui_settings()
    settings.setValue(RECENT_ROOMS_KEY, rooms[:MAX_RECENT_ROOMS])
    settings.sync()


# Rooms of other SylkServer domains
#
# A room is given as a link (https://host/conference/room, as the Sylk web app shares them) or
# as room@host. The host's SylkServer configuration (discovery, _sylkserver.<domain>) says where
# its server is: wsServer, iceServers, defaultConferenceDomain (the video rooms, at
# videoconference.<domain>) and defaultGuestDomain. The same room's MSRP switch is always
# room@conference.<domain>.

def parse_room(text):
    """(room, host) of what was typed or pasted: a link https://host/conference/room, sip:room@host,
    room@host; host is None for a bare room name; None when there is nothing."""
    text = (text or '').strip()
    if not text:
        return None
    if re.match(r'^https?://', text, re.IGNORECASE):
        parts = urlsplit(text)
        segments = [segment for segment in parts.path.split('/') if segment]
        if not segments or not parts.hostname:
            return None
        room = segments[segments.index('conference') + 1] if 'conference' in segments[:-1] else segments[-1]
        return unquote(room).strip(), parts.hostname.lower()
    text = re.sub('^sips?:', '', text, flags=re.IGNORECASE)
    room, _, host = text.partition('@')
    host = host.partition(';')[0].partition(':')[0].strip().lower()
    return room.strip(), host or None


def _configuration_domain(host):
    """The domain whose SylkServer configuration covers a room host."""
    host = host.lower()
    for prefix in ('videoconference.', 'conference.'):
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


class RoomTarget(object):
    """Where a room of a SylkServer domain is."""

    def __init__(self, room, domain, configuration, account):
        self.room = room
        self.domain = domain
        self.ws_server = configuration.get('wsServer')
        self.ice_servers = configuration.get('iceServers') or []
        video_domain = str(configuration.get('defaultConferenceDomain') or 'videoconference.%s' % domain).lower()
        self.video_uri = '%s@%s' % (room, video_domain)
        sip_domain = 'conference.' + video_domain[len('videoconference.'):] if video_domain.startswith('videoconference.') else 'conference.%s' % domain
        self.sip_uri = '%s@%s' % (room, sip_domain)       # the MSRP switch of the same room
        self.guest_domain = configuration.get('defaultGuestDomain') or 'guest.%s' % domain
        # another server than the account's own: joined there, as ourselves or as a guest
        self.remote = self.ws_server != ws_server(account)

    def __repr__(self):
        return f'<RoomTarget {self.video_uri} / {self.sip_uri} at {self.ws_server}{" (remote)" if self.remote else ""}>'


def room_target(room, host, account):
    """The RoomTarget of room at host from the cached configuration of its domain, or None."""
    if not room or not host:
        return None
    from blink.sylk_discovery import SylkServerDiscovery
    domain = _configuration_domain(host)
    configuration = SylkServerDiscovery().configuration(domain)
    if not isinstance(configuration, dict):
        return None
    server = configuration.get('wsServer')
    if not isinstance(server, str) or not server.strip().startswith(('wss://', 'ws://')):
        return None
    return RoomTarget(room, domain, configuration, account)


def local_room_host(host, account):
    """Whether a room host is the account's own conference server (nothing to look up)."""
    if not host or account is None or account is BonjourAccount():
        return True
    host = host.lower()
    own = str(getattr(account.server, 'conference_server', None) or '').lower()
    return host == own or _configuration_domain(host) == account.id.domain.lower()


def room_video(uri, account):
    """(the video room address, its RoomTarget or None) to add video to the room uri (its MSRP switch
    address) from account, from what is known now; None when there is no video for it."""
    from blink.conference import split_uri
    user, host = split_uri(uri)
    if not user or not host:
        return None
    if not local_room_host(host, account):
        # a room of another SylkServer: its conference. or videoconference. host (not someone at that domain)
        if not host.lower().startswith(('conference.', 'videoconference.')):
            return None
        target = room_target(user, host, account)
        if target is not None and target.remote:
            return (target.video_uri, target) if media_available() else None
        return None
    if conference_video_available(account):
        return '%s@%s' % (user, host), None
    return None


DISCOVERY_TIMEOUT = 12000      # ms


def resolve_room(room, host, account, callback):
    """callback(RoomTarget or None) once the configuration of host's domain is known: at once when
    cached, else after discovery (in the GUI thread)."""
    target = room_target(room, host, account)
    if target is not None or not room or not host:
        callback(target)
        return
    from PyQt6.QtCore import QTimer
    from blink.logging import ActivityLog
    from blink.sylk_discovery import SylkServerDiscovery
    domain = _configuration_domain(host)
    ActivityLog().info(f'[webrtc] Looking up the SylkServer of {domain} for the room {room}@{host}')
    discovery = SylkServerDiscovery()
    discovery.discover(domain, force=True)
    timer = QTimer()
    timer.setInterval(200)
    waited = [0]

    def check():
        waited[0] += timer.interval()
        if domain in discovery.running and waited[0] < DISCOVERY_TIMEOUT:
            return
        timer.stop()
        _pending_timers.discard(timer)
        result = room_target(room, host, account)
        ActivityLog().info(f'[webrtc] {domain}: ' + (f'SylkServer at {result.ws_server}' if result is not None else 'no SylkServer found'))
        callback(result)

    timer.timeout.connect(check)
    timer.start()
    _pending_timers.add(timer)


_pending_timers = set()     # the timers of discoveries waited for, kept alive
