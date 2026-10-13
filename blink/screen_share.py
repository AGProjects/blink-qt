"""Screen sharing in video calls -- the call side.

The screen is shared as a video device ("My screen" in the camera list),
so a shared screen is an ordinary video call that any client can show. On
Linux the device captures through the desktop portal: sharing asks the
desktop for a monitor (its picker shows, unless the restore token saved from
the previous time lets it skip that) and only once one was picked is the
device selected. Wire format and geometry are in screen_pointer.py. This
module:

  * advertises our capabilities when a call starts, and records the peer's;
  * tells the peer when we start/stop sending a screen and records when the
    peer does;
  * runs the screen request handshake: asks the peer for its screen, and
    prompts when the peer asks for ours -- accepting shares the screen, and
    the camera comes back when the call or its video ends;
  * as the SHARER, draws the pointers the peer sends over the monitor being
    shared (PointerOverlay) and acks them;
  * as the VIEWER, sends pointers for the video window and reports the acks
    back to it.

Per-call state lives here, keyed by the SIP session, not in the video
window, so it survives the window being closed and reopened mid-call. The
video window listens for BlinkScreenPointerDidChange,
BlinkScreenPointerGotAck (data.x, data.y), BlinkScreenShareRequestDidResolve
(data.result is accepted, rejected or timeout) and BlinkScreenShareNotice
(data.text), all posted with the SIP session as sender.
"""

import time

from PyQt6.QtCore import Qt, QPointF, QSettings, QTimer, QVariantAnimation
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.application import SIPApplication
from sipsimple.configuration.datatypes import VideoResolution
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from blink import screen_pointer as SP
from blink.logging import ActivityLog
from blink.pstn_normalize import check_valid_phone_number, is_conference_uri
from blink.util import call_later, run_in_gui_thread, translate

try:
    from sipsimple.core import (screen_capture_info, screen_capture_restore_token, set_screen_capture_restore_token,
                                request_screen_capture, reset_screen_capture)
except ImportError:     # an SDK without the Linux screen capture device
    screen_capture_info = lambda: dict(state='unavailable', position=None, size=None, source_size=None)
    screen_capture_restore_token = lambda: None
    set_screen_capture_restore_token = lambda token: None
    request_screen_capture = lambda: False
    reset_screen_capture = lambda: None


__all__ = ['ScreenShareManager']


MARKER_RADIUS = 26.0
DOT_RADIUS = 9.0
PULSE_SECONDS = 0.38
PULSES = 2
FADE_SECONDS = 0.2
PENDING_CLICK_TTL = 5.0         # seconds a sent click waits for its ack
POLL_INTERVAL = 500             # ms, watching the desktop's answer and the capture

RESTORE_TOKEN_KEY = 'screen_sharing/restore_token'
CAMERA_BEFORE_KEY = 'screen_sharing/camera_before'
RESOLUTION_BEFORE_KEY = 'screen_sharing/resolution_before'

# A screen is sent sharper than a camera: captured and encoded at this size
# and bitrate while it is shared, during the call (no renegotiation: the
# encoder is reopened at the new size, see VideoStream.set_encoder_size).
SCREEN_RESOLUTION = VideoResolution('1280x720')
SCREEN_BITRATE = 1.5e6          # bits per second

# Camera resolutions offered in the video window, with the bitrate (Mbps)
# each one is sent at. Changing it applies to the calls going on at once.
CAMERA_RESOLUTIONS = [(VideoResolution('640x480'), '480p', 0.8),
                      (VideoResolution('1280x720'), '720p', 1.5),
                      (VideoResolution('1920x1080'), '1080p', 2.5)]

BLUE = QColor(33, 150, 243)


def log(text):
    ActivityLog().info('[screen sharing] %s' % text)


class _CallState(object):
    def __init__(self):
        self.peer_capabilities = None       # None: the peer never advertised
        self.peer_sharing = False
        self.peer_in_app = True
        self.capabilities_sent = False
        self.sharing_signalled = False      # we told the peer 'start'
        self.pending_clicks = {}            # t -> (nx, ny, sent_at)
        self.last_drawn_click = None
        # screen request, ours
        self.request_id = None
        # screen request, the peer's
        self.handled_requests = set()
        self.prompt = None                  # (request_id, message box)
        self.awaiting_share = False         # accepted, the screen is not on yet
        self.encoder = None                 # (w, h, bps) the call's video is encoded at


def _content_of(data):
    """(content_type, body) of an in-dialog MESSAGE, looking inside CPIM."""
    headers = data.headers or {}
    header = headers.get('Content-Type')
    if header is None:
        return None, None
    content_type = (getattr(header, 'content_type', None) or str(header)).lower()
    body = data.body
    if content_type == 'message/cpim':
        try:
            from sipsimple.streams.msrp.chat import CPIMPayload
            payload = CPIMPayload.decode(body)
        except Exception:
            return None, None
        return (payload.content_type or '').lower(), payload.content
    return content_type, body


def _video_established(session):
    return any(stream.type == 'video' and getattr(stream, 'state', None) == 'ESTABLISHED'
               for stream in (session.streams or []))


def _local_device():
    device = getattr(SIPApplication, 'video_device', None)
    try:
        return device.real_name if device is not None else None
    except Exception:
        return None


def _local_on_screen():
    return SP.is_screen_device(_local_device())


def _screen_devices():
    try:
        devices = SIPApplication.engine.video_devices
    except Exception:
        return []
    return sorted((device for device in devices if SP.is_screen_device(device)), key=SP.screen_index)


def _select_device(device):
    """Select the video device; the screen also gets the screen resolution,
    a camera gets back the resolution it had before the screen."""
    settings = SIPSimpleSettings()
    qsettings = QSettings()
    modified = False
    if SP.is_screen_device(device):
        if str(settings.video.resolution) != str(SCREEN_RESOLUTION):
            if not qsettings.value(RESOLUTION_BEFORE_KEY, ''):
                qsettings.setValue(RESOLUTION_BEFORE_KEY, str(settings.video.resolution))
            settings.video.resolution = SCREEN_RESOLUTION
            modified = True
    else:
        before = qsettings.value(RESOLUTION_BEFORE_KEY, '')
        if before:
            qsettings.remove(RESOLUTION_BEFORE_KEY)
            if str(settings.video.resolution) != before:
                settings.video.resolution = VideoResolution(before)
                modified = True
    if settings.video.device != device:
        settings.video.device = device
        modified = True
    if modified:
        settings.save()


def _video_streams(session):
    return [stream for stream in (session.streams or []) if stream.type == 'video'
            and getattr(stream, 'state', None) == 'ESTABLISHED' and hasattr(stream, 'set_encoder_size')]


# The marker on the shared monitor

class PointerOverlay(QWidget):
    """Pulsing marker drawn above everything on the monitor we share, the
    same marker sylk-mobile's PointerGuideOverlay draws: a blue dot with a
    ring that grows and fades twice, then the whole marker fades out.

    It is a click-through window over the monitor, so it is captured with
    the screen and the viewer sees the marker in the video too.
    """

    def __init__(self):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool |
                 Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.wayland = QGuiApplication.platformName().startswith('wayland')
        if not self.wayland:
            flags |= Qt.WindowType.X11BypassWindowManagerHint     # above full screen windows too
        super(PointerOverlay, self).__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setWindowTitle('Blink pointer')
        self.point = None
        self.progress = 1.0
        self.animation = QVariantAnimation(self)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setDuration(int((PULSE_SECONDS * PULSES + FADE_SECONDS) * 1000))
        self.animation.valueChanged.connect(self._SH_AnimationValueChanged)
        self.animation.finished.connect(self.hide)

    def show_marker(self, screen, sx, sy):
        """sx/sy normalized on the screen, origin top-left."""
        geometry = screen.geometry()
        if self.wayland:
            # a Wayland client cannot place a window: cover the monitor instead
            if self.windowHandle() is None:
                self.create()
            self.windowHandle().setScreen(screen)
            self.point = QPointF(sx * geometry.width(), sy * geometry.height())
            self.showFullScreen()
        else:
            self.setGeometry(geometry)
            self.point = QPointF(sx * geometry.width(), sy * geometry.height())
            self.show()
        self.raise_()
        self.animation.stop()
        self.animation.start()

    def _SH_AnimationValueChanged(self, value):
        self.progress = value
        self.update()

    def paintEvent(self, event):
        if self.point is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        elapsed = self.progress * (PULSE_SECONDS * PULSES + FADE_SECONDS)
        pulses_over = PULSE_SECONDS * PULSES
        if elapsed < pulses_over:
            phase = (elapsed % PULSE_SECONDS) / PULSE_SECONDS
            phase = 1 - (1 - phase) ** 2            # ease out
            scale = 0.6 + (2.3 - 0.6) * phase
            ring = QColor(BLUE)
            ring.setAlphaF(0.9 * (1 - phase))
            painter.setPen(QPen(ring, 3.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            radius = MARKER_RADIUS * scale
            painter.drawEllipse(self.point, radius, radius)
            opacity = 1.0
        else:
            opacity = max(0.0, 1 - (elapsed - pulses_over) / FADE_SECONDS)
        dot = QColor(BLUE)
        dot.setAlphaF(0.92 * opacity)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(self.point, DOT_RADIUS, DOT_RADIUS)
        painter.end()


def _close(first, second, slack=2):
    return abs(first[0] - second[0]) <= slack and abs(first[1] - second[1]) <= slack


def _screen_sizes(screen):
    """Logical and physical (device pixel) size of a QScreen."""
    geometry = screen.geometry()
    ratio = screen.devicePixelRatio() or 1
    return (geometry.width(), geometry.height()), (round(geometry.width() * ratio), round(geometry.height() * ratio))


def _shared_qscreen(info, why=None):
    """The QScreen the desktop's position/size of the shared monitor is, or
    a best guess. Under XWayland with scaling the desktop's logical
    coordinates and Qt's can differ, so it goes from exact to loose: the
    position, then the size (logical or in pixels, also the size of the
    frames captured), then the shape, then the primary screen. why, a list,
    gets how the screen was found."""
    why = why if why is not None else []
    application = QApplication.instance()
    screens = application.screens() if application is not None else []
    if not screens:
        return None
    if len(screens) == 1:
        why.append('the only screen')
        return screens[0]
    position, size, source = info.get('position'), info.get('size'), info.get('source_size')
    if position is not None:
        position = tuple(position)
        for screen in screens:
            geometry = screen.geometry()
            if (geometry.x(), geometry.y()) == position:
                why.append('position')
                return screen
        for screen in screens:
            ratio = screen.devicePixelRatio() or 1
            geometry = screen.geometry()
            if _close((round(geometry.x() * ratio), round(geometry.y() * ratio)), position):
                why.append('position in pixels')
                return screen
    for name, wanted in (('size', size), ('captured size', source)):
        if wanted is None:
            continue
        wanted = tuple(wanted)
        matches = [screen for screen in screens if any(_close(sizes, wanted) for sizes in _screen_sizes(screen))]
        if len(matches) == 1:
            why.append(name)
            return matches[0]
    for name, wanted in (('shape', size), ('captured shape', source)):
        if wanted is None or wanted[0] <= 0 or wanted[1] <= 0:
            continue
        aspect = wanted[0] / wanted[1]
        same_shape = [screen for screen in screens if screen.geometry().height() and
                      abs(screen.geometry().width() / screen.geometry().height() - aspect) < 0.01]
        if len(same_shape) == 1:
            why.append(name)
            return same_shape[0]
    why.append('no match, primary screen')
    return application.primaryScreen()


def _describe_screens():
    parts = []
    for screen in QApplication.instance().screens():
        geometry = screen.geometry()
        logical, physical = _screen_sizes(screen)
        parts.append('%s at %d,%d %dx%d (%dx%d px)' % (screen.name(), geometry.x(), geometry.y(), logical[0], logical[1], physical[0], physical[1]))
    return ', '.join(parts)


# The manager

@implementer(IObserver)
class ScreenShareManager(object, metaclass=Singleton):

    def __init__(self):
        self.calls = {}
        self.overlay = None
        self.capture_wanted = False         # asked the desktop for the screen, not on the wire yet
        self.device_before_share = None     # settings.video.device to put back
        self.device_at_call_start = None    # settings.video.device when the first of the current calls started
        self.pointer_screen_logged = None   # the screen pointers were last drawn on, for the log
        self.saved_token = None
        self.unwanted_ready = False         # a picked screen nobody wants, seen at the last poll
        self.poll_timer = QTimer()
        self.poll_timer.setInterval(POLL_INTERVAL)
        self.poll_timer.timeout.connect(self._SH_PollTimerTimeout)
        self.notification_center = NotificationCenter()
        for name in ('SIPApplicationDidStart', 'SIPSessionDidStart', 'SIPSessionDidEnd', 'SIPSessionDidFail',
                     'SIPSessionGotMessage', 'MediaStreamDidStart', 'MediaStreamDidEnd', 'VideoDeviceDidChangeCamera'):
            self.notification_center.add_observer(self, name=name)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        try:
            handler(notification.sender, notification.data)
        except Exception as e:
            ActivityLog().error('[screen sharing] %s handler failed: %s' % (notification.name, e))

    # queries for the video window

    def can_share_screen(self):
        """The screen can be sent as video here."""
        return bool(_screen_devices())

    @property
    def sharing(self):
        """Our screen is (being made) the video we send."""
        return _local_on_screen() or self.capture_wanted

    def can_point(self, session):
        """The peer is sending its screen and draws the pointers we send."""
        state = self.calls.get(session)
        return bool(state is not None and state.peer_sharing and state.peer_capabilities
                    and SP.CAP_POINTER in state.peer_capabilities)

    def can_request_screen(self, session):
        """The peer can send a screen and understands being asked, the call has
        video, and neither side is sharing already."""
        state = self.calls.get(session)
        capabilities = (state.peer_capabilities or []) if state is not None else []
        return bool(state is not None
                    and SP.CAP_SCREEN_SHARING in capabilities
                    and SP.CAP_SCREEN_REQUEST in capabilities
                    and session.state == 'connected' and _video_established(session)
                    and not state.peer_sharing and not state.sharing_signalled)

    def screen_request_pending(self, session):
        state = self.calls.get(session)
        return state is not None and state.request_id is not None

    def peer_sharing(self, session):
        state = self.calls.get(session)
        return state is not None and state.peer_sharing

    def peer_in_app(self, session):
        state = self.calls.get(session)
        return state is None or state.peer_in_app

    def send_screen_request(self, session):
        state = self.calls.get(session)
        if state is None or state.request_id is not None or not self.can_request_screen(session):
            return False
        request_id, body = SP.build_request()
        state.request_id = request_id
        self._send(session, SP.SCREEN_SHARING_CONTENT_TYPE, body)
        log('asked %s for the screen (%s)' % (session.remote_identity.uri, request_id))
        call_later(SP.REQUEST_TTL, self._request_expired, session, request_id)
        self._changed(session)
        return True

    def send_pointer(self, session, nx, ny):
        state = self.calls.get(session)
        if state is None or not self.can_point(session) or not state.peer_in_app:
            return None
        t = SP.new_click_id()
        now = t / 1000.0
        state.pending_clicks = dict((key, value) for key, value in state.pending_clicks.items()
                                    if now - value[2] < PENDING_CLICK_TTL)
        state.pending_clicks[t] = (nx, ny, now)
        self._send(session, SP.POINTER_CONTENT_TYPE, SP.build_pointer(nx, ny, t))
        return t

    # sharing our screen

    def share_screen(self):
        """Send the screen instead of the camera. On Linux the desktop asks
        which monitor first (or not, with a restore token): the device is
        selected once it answers. False when the screen cannot be shared."""
        devices = _screen_devices()
        if not devices:
            return False
        current = SIPSimpleSettings().video.device
        if not SP.is_screen_device(current) and not self.capture_wanted:
            self.device_before_share = current
            QSettings().setValue(CAMERA_BEFORE_KEY, current or '')
        state = screen_capture_info()['state']
        if state == 'unavailable':
            # the device opens by itself (or falls back to a camera)
            self.capture_wanted = True
            log('sharing the screen, video switched to %s' % devices[0])
            _select_device(devices[0])
            self.poll_timer.start()
            return True
        if state == 'capturing' or _local_on_screen():
            return True
        reset_screen_capture()      # a denied/failed/ended outcome from before
        if not request_screen_capture():
            log('the desktop cannot be asked for the screen')
            return False
        self.capture_wanted = True
        log('asking the desktop which screen to share')
        self.poll_timer.start()
        return True

    def stop_sharing(self, camera=None):
        """Back to the camera that was on before (or to the one given)."""
        self.capture_wanted = False
        reset_screen_capture()
        if _local_on_screen() or (camera is not None and not SP.is_screen_device(camera)):
            camera = camera or self.device_before_share or QSettings().value(CAMERA_BEFORE_KEY, '') or 'system_default'
            log('sharing over, video back to %s' % camera)
            _select_device(camera)
        self.device_before_share = None
        QSettings().remove(CAMERA_BEFORE_KEY)
        for state in self.calls.values():
            state.awaiting_share = False

    # sending

    @run_in_thread('network-io')
    def _send(self, session, content_type, content):
        try:
            session.send_message(content_type, content)
        except Exception as e:
            log('cannot send %s: %s' % (content_type, e))

    def _eligible(self, session):
        """Not for conferences (the mixer is no client) nor for phone numbers
        (a gateway, which has no use for these), as on sylk-mobile."""
        if getattr(session, 'remote_focus', False):
            return False
        try:
            uri = session.remote_identity.uri
            if check_valid_phone_number(uri.user):
                return False
            return not is_conference_uri('%s@%s' % (uri.user, uri.host), getattr(session, 'account', None))
        except Exception:
            return True

    def _state(self, session):
        state = self.calls.get(session)
        if state is None:
            state = self.calls[session] = _CallState()
        return state

    def _changed(self, session):
        self.notification_center.post_notification('BlinkScreenPointerDidChange', sender=session)

    def _notice(self, session, text):
        self.notification_center.post_notification('BlinkScreenShareNotice', sender=session, data=NotificationData(text=text))

    def _update_sharing(self, session):
        state = self.calls.get(session)
        if state is None or not self._eligible(session):
            return
        sharing = session.state == 'connected' and _video_established(session) and _local_on_screen()
        if sharing == state.sharing_signalled:
            return
        state.sharing_signalled = sharing
        state.last_drawn_click = None
        if sharing:
            state.awaiting_share = False
        self._send(session, SP.SCREEN_SHARING_CONTENT_TYPE, SP.build_sharing('start' if sharing else 'stop'))
        log('told %s we %s sharing the screen' % (session.remote_identity.uri, 'started' if sharing else 'stopped'))
        if not self._anyone_sharing() and self.overlay is not None:
            self.overlay.hide()

    def _anyone_sharing(self):
        return any(state.sharing_signalled for state in self.calls.values())

    def _any_video_call(self):
        return any(_video_established(session) for session in self.calls)

    def _call_video_over(self):
        """A call or its video ended: the screen is not shared with nobody."""
        if self.sharing and not self._any_video_call():
            if SP.is_screen_device(self.device_at_call_start) and not self.device_before_share:
                # the call started on the screen: it stays the video device, only the capture ends
                self.capture_wanted = False
                reset_screen_capture()
                for state in self.calls.values():
                    state.awaiting_share = False
            else:
                self.stop_sharing()

    def _calls_over(self):
        """The last call ended: the screen is remembered as the video device
        only when the call started with it, otherwise the camera comes back."""
        start_device, self.device_at_call_start = self.device_at_call_start, None
        if SP.is_screen_device(start_device):
            return
        if not SP.is_screen_device(SIPSimpleSettings().video.device) and not self.capture_wanted:
            return
        camera = self.device_before_share or QSettings().value(CAMERA_BEFORE_KEY, '') or start_device or 'system_default'
        if SP.is_screen_device(camera):
            camera = 'system_default'
        self.capture_wanted = False
        reset_screen_capture()
        log('calls over, video back to %s' % camera)
        _select_device(camera)
        self.device_before_share = None
        QSettings().remove(CAMERA_BEFORE_KEY)

    # the desktop's answer, the capture going on

    def _SH_PollTimerTimeout(self):
        info = screen_capture_info()
        state = info['state']
        unwanted_ready, self.unwanted_ready = self.unwanted_ready, False
        if state == 'ready':
            if self.capture_wanted and not _local_on_screen():
                devices = _screen_devices()
                if devices:
                    log('the desktop gave us a screen, video switched to %s' % devices[0])
                    _select_device(devices[0])
            elif not self.capture_wanted and not _local_on_screen():
                # ready also for a moment while the SDK reopens the device (a new
                # size, say): only a screen nobody wanted twice in a row goes
                if unwanted_ready:
                    reset_screen_capture()
                else:
                    self.unwanted_ready = True
        elif state == 'idle' and self.capture_wanted and not _local_on_screen():
            # the desktop's answer came to nothing (or was given up)
            log('the screen could not be captured')
            self._share_failed(translate('screen_share', 'Your screen could not be shared'))
        elif state == 'capturing':
            self._save_restore_token()
        elif state in ('denied', 'failed'):
            reset_screen_capture()
            if self.capture_wanted:
                if state == 'denied':
                    log('no screen was picked')
                    text = translate('screen_share', 'Screen sharing was cancelled')
                else:
                    log('the screen could not be captured')
                    text = translate('screen_share', 'Your screen could not be shared')
                self._share_failed(text)
        elif state == 'ended':
            reset_screen_capture()
            if self.capture_wanted or _local_on_screen():
                log('sharing was stopped from the desktop')
                for session, call_state in self.calls.items():
                    if call_state.sharing_signalled:
                        self._notice(session, translate('screen_share', 'Screen sharing was stopped'))
                self.stop_sharing()
        if not self.capture_wanted and not _local_on_screen():
            self.poll_timer.stop()

    def _share_failed(self, text):
        for session, call_state in self.calls.items():
            if call_state.awaiting_share or _video_established(session):
                self._notice(session, text)
        self.stop_sharing()

    def _save_restore_token(self):
        token = screen_capture_restore_token()
        if token and token != self.saved_token:
            self.saved_token = token
            QSettings().setValue(RESTORE_TOKEN_KEY, token)

    # notifications

    def _NH_SIPApplicationDidStart(self, sender, data):
        settings = QSettings()
        token = settings.value(RESTORE_TOKEN_KEY, '') or None
        if token:
            self.saved_token = token
            set_screen_capture_restore_token(token)
        # quit while sharing: the screen is not what a new start sends
        camera_before = settings.value(CAMERA_BEFORE_KEY, '') or None
        sip_settings = SIPSimpleSettings()
        if SP.is_screen_device(sip_settings.video.device) or (camera_before and sip_settings.video.device == 'system_default'):
            _select_device(camera_before or 'system_default')
        elif settings.value(RESOLUTION_BEFORE_KEY, ''):
            _select_device(sip_settings.video.device)       # puts the resolution back
        settings.remove(CAMERA_BEFORE_KEY)

    def _NH_SIPSessionDidStart(self, session, data):
        if not self.calls:
            self.device_at_call_start = SIPSimpleSettings().video.device
        state = self._state(session)
        if self._eligible(session) and not state.capabilities_sent:
            state.capabilities_sent = True
            capabilities = SP.my_capabilities(can_share_screen=self.can_share_screen())
            self._send(session, SP.CAPABILITIES_CONTENT_TYPE, SP.build_capabilities(capabilities))
        self._update_sharing(session)

    def _NH_SIPSessionDidEnd(self, session, data):
        state = self.calls.pop(session, None)
        if state is not None:
            self._dismiss_prompt(state)
            self._changed(session)
        if not self._anyone_sharing() and self.overlay is not None:
            self.overlay.hide()
        self._call_video_over()
        if state is not None and not self.calls:
            self._calls_over()

    _NH_SIPSessionDidFail = _NH_SIPSessionDidEnd

    def _NH_MediaStreamDidStart(self, stream, data):
        if stream.type == 'video' and stream.session is not None and stream.session in self.calls:
            # a new stream encodes at the settings of the moment, but above
            # VGA the negotiated H.264 level (3.0 from Blink for macOS) can
            # have clamped it smaller: set it to be sure
            settings = SIPSimpleSettings()
            size = settings.video.resolution
            current = (size.width, size.height, settings.video.max_bitrate * 1e6 if settings.video.max_bitrate else None)
            self.calls[stream.session].encoder = current if size.width * size.height <= 640 * 480 else None
            self._resize_encoders(stream.session)       # the screen, say
            self._update_sharing(stream.session)

    def _NH_MediaStreamDidEnd(self, stream, data):
        session = getattr(stream, 'session', None)
        if stream.type != 'video' or session not in self.calls:
            return
        state = self.calls[session]
        if state.peer_sharing:
            # no video, no screen: the peer may never get to send 'stop'
            state.peer_sharing = False
            self._changed(session)
        if session.state == 'connected':
            self._update_sharing(session)
        else:
            state.sharing_signalled = False
        self._dismiss_prompt(state)
        state.awaiting_share = False
        state.encoder = None                # a new video stream starts at the settings
        self._call_video_over()

    @staticmethod
    def _encoder_target():
        """(w, h, bps) to encode at: the screen's while it is shared, the
        video settings' otherwise."""
        if _local_on_screen():
            return SCREEN_RESOLUTION.width, SCREEN_RESOLUTION.height, SCREEN_BITRATE
        settings = SIPSimpleSettings()
        size = settings.video.resolution
        bitrate = settings.video.max_bitrate * 1e6 if settings.video.max_bitrate else None
        return size.width, size.height, bitrate

    def _resize_encoders(self, session=None):
        """Bring the encoder of the calls going on (or of one) to the target,
        during the call: the other side gets a keyframe at the new size."""
        width, height, bitrate = target = self._encoder_target()
        for call in ([session] if session is not None else list(self.calls)):
            state = self.calls.get(call)
            if state is None or state.encoder == target:
                continue        # None: not known (video from before), set it
            for stream in _video_streams(call):
                try:
                    if stream.set_encoder_size(width, height, bitrate):
                        state.encoder = target
                        log('%s: encoding at %dx%d, %.1f Mbps' % (call.remote_identity.uri, width, height, (bitrate or 0) / 1e6))
                except Exception as e:
                    log('%s: cannot encode at %dx%d: %s' % (call.remote_identity.uri, width, height, e))

    def set_camera_resolution(self, resolution):
        """Send the camera at another resolution (one of CAMERA_RESOLUTIONS),
        in the calls going on too. Not while the screen is shared."""
        if self.sharing:
            return False
        for size, label, bitrate in CAMERA_RESOLUTIONS:
            if size == resolution:
                break
        else:
            return False
        from blink.configuration.settings import H264_LEVELS
        settings = SIPSimpleSettings()
        settings.video.resolution = size
        settings.video.max_bitrate = bitrate
        settings.video.h264.level = H264_LEVELS.get(str(size), '3.1')       # what new calls offer
        settings.save()     # the camera reopens at the new size
        log('camera resolution %s (%s, %.1f Mbps)' % (size, label, bitrate))
        self._resize_encoders()
        return True

    def _NH_VideoDeviceDidChangeCamera(self, sender, data):
        on_screen = _local_on_screen()
        self._resize_encoders()
        for session in list(self.calls):
            self._update_sharing(session)
        if on_screen:
            self.capture_wanted = False         # it is on the wire now
            self.poll_timer.start()             # until it ends: the desktop can stop it
            for state in self.calls.values():
                state.awaiting_share = False
            return
        capture_state = screen_capture_info()['state']
        if self.capture_wanted and capture_state in ('pending', 'ready'):
            return                              # still waiting for the desktop
        if self.capture_wanted and capture_state in ('unavailable', 'idle', 'failed'):
            # the device could not open (no permission to capture, say) and
            # the SDK fell back to a camera
            log('the screen could not be captured')
            self._share_failed(translate('screen_share', 'Your screen could not be shared'))
            return
        if self.capture_wanted:
            return
        # back on a camera by hand: nothing left to put back
        self.device_before_share = None
        QSettings().remove(CAMERA_BEFORE_KEY)

    def _NH_SIPSessionGotMessage(self, session, data):
        content_type, body = _content_of(data)
        if content_type not in SP.CONTENT_TYPES:
            return
        state = self._state(session)
        peer = session.remote_identity.uri

        if content_type == SP.CAPABILITIES_CONTENT_TYPE:
            capabilities = SP.parse_capabilities(body)
            if capabilities is not None:
                state.peer_capabilities = capabilities
                log('%s advertised: %s' % (peer, ', '.join(capabilities) or '(none)'))
                self._changed(session)

        elif content_type == SP.SCREEN_SHARING_CONTENT_TYPE:
            signal = SP.parse_sharing_signal(body)
            action = signal['action'] if signal is not None else None
            if action in ('start', 'stop'):
                state.peer_sharing = (action == 'start')
                if state.peer_sharing:
                    state.peer_in_app = True
                    state.request_id = None     # asked or not, the screen is here
                log('%s %s sharing the screen' % (peer, 'started' if state.peer_sharing else 'stopped'))
                self._changed(session)
            elif action == 'request':
                self._got_screen_request(session, state, signal)
            elif action in ('request_accept', 'request_reject'):
                if signal['id'] == state.request_id:
                    state.request_id = None
                    result = 'accepted' if action == 'request_accept' else 'rejected'
                    log('%s %s our request' % (peer, result))
                    self.notification_center.post_notification('BlinkScreenShareRequestDidResolve', sender=session,
                                                               data=NotificationData(result=result))
                    self._changed(session)

        elif content_type == SP.POINTER_CONTENT_TYPE:
            pointer = SP.parse_pointer(body)
            if pointer is not None and state.sharing_signalled:
                x, y, t = pointer
                if t is None or t != state.last_drawn_click:
                    state.last_drawn_click = t
                    self._draw(x, y)
                if t is not None:
                    self._send(session, SP.POINTER_ACK_CONTENT_TYPE, SP.build_ack(t))

        elif content_type == SP.POINTER_ACK_CONTENT_TYPE:
            t = SP.parse_ack(body)
            click = state.pending_clicks.pop(t, None) if t is not None else None
            if click is not None:
                self.notification_center.post_notification('BlinkScreenPointerGotAck', sender=session,
                                                           data=NotificationData(x=click[0], y=click[1]))

        elif content_type == SP.POINTER_VISIBILITY_CONTENT_TYPE:
            in_app = SP.parse_visibility(body)
            if in_app is not None:
                state.peer_in_app = in_app
                self._changed(session)

    # screen request

    def _request_expired(self, session, request_id):
        state = self.calls.get(session)
        if state is None or state.request_id != request_id:
            return
        state.request_id = None
        log('%s did not answer' % session.remote_identity.uri)
        self.notification_center.post_notification('BlinkScreenShareRequestDidResolve', sender=session,
                                                   data=NotificationData(result='timeout'))
        self._changed(session)

    def _reply(self, session, action, request_id):
        self._send(session, SP.SCREEN_SHARING_CONTENT_TYPE, SP.build_request_reply(action, request_id))

    def _got_screen_request(self, session, state, signal):
        request_id = signal['id']
        peer = session.remote_identity.uri
        if request_id in state.handled_requests:
            return      # the same MESSAGE delivered twice
        state.handled_requests.add(request_id)
        expires = SP.parse_expires(signal['expires'])
        if expires is None or time.time() >= expires:
            log('dropped a stale request from %s' % peer)
            return
        if state.sharing_signalled:
            self._reply(session, 'request_accept', request_id)
            return
        if not self.can_share_screen() or not self._eligible(session):
            self._reply(session, 'request_reject', request_id)
            return
        log('%s asks for our screen (%s)' % (peer, request_id))
        self._dismiss_prompt(state)
        self._prompt(session, state, request_id, expires)

    @staticmethod
    def _blink_session(session):
        from blink.sessions import SessionManager
        for blink_session in SessionManager().sessions:
            if blink_session.sip_session is session:
                return blink_session
        return None

    def _peer_name(self, session):
        blink_session = self._blink_session(session)
        name = blink_session.contact.name if blink_session is not None and blink_session.contact is not None else None
        if not name:
            identity = session.remote_identity
            name = identity.display_name or '%s@%s' % (identity.uri.user, identity.uri.host)
        return name

    def _prompt_parent(self, session):
        application = QApplication.instance()
        blink_session = self._blink_session(session)
        manager = getattr(application, 'video_window_manager', None)
        window = manager.window_for(blink_session) if manager is not None and blink_session is not None else None
        if window is not None and window.isVisible():
            return window
        return getattr(application, 'main_window', None)

    def _prompt(self, session, state, request_id, expires):
        parent = self._prompt_parent(session)
        box = QMessageBox(QMessageBox.Icon.Question, translate('screen_share', 'Share your screen'),
                          translate('screen_share', '%s would like to see your screen.') % self._peer_name(session),
                          QMessageBox.StandardButton.NoButton, parent)
        box.setInformativeText(translate('screen_share', 'If you accept, everything on the screen you pick, notifications and other applications included, '
                                                         'becomes visible until you pick a camera again.'))
        accept = box.addButton(translate('screen_share', 'Accept'), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(translate('screen_share', 'Reject'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(accept)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        state.prompt = (request_id, box)

        def answered(code):
            if state.prompt is None or state.prompt[0] != request_id:
                return      # expired or dismissed: no reply
            state.prompt = None
            self._answer(session, state, request_id, box.clickedButton() is accept)

        box.finished.connect(answered)
        box.setWindowModality(Qt.WindowModality.WindowModal if parent is not None else Qt.WindowModality.NonModal)
        box.open()
        box.raise_()
        box.activateWindow()
        call_later(max(0.0, expires - time.time()), self._prompt_expired, state, request_id)

    def _prompt_expired(self, state, request_id):
        if state.prompt is not None and state.prompt[0] == request_id:
            log('request %s expired unanswered' % request_id)
            self._dismiss_prompt(state)

    def _dismiss_prompt(self, state):
        prompt, state.prompt = state.prompt, None
        if prompt is None:
            return
        request_id, box = prompt
        try:
            box.close()
        except RuntimeError:
            pass        # already gone

    def _answer(self, session, state, request_id, accepted):
        if self.calls.get(session) is not state:
            return      # the call is gone
        if not accepted:
            log('rejected the request of %s' % session.remote_identity.uri)
            self._reply(session, 'request_reject', request_id)
            return
        self._reply(session, 'request_accept', request_id)
        if state.sharing_signalled:
            return
        log('accepted the request of %s' % session.remote_identity.uri)
        state.awaiting_share = True
        if not self.share_screen():
            state.awaiting_share = False
            self._notice(session, translate('screen_share', 'Your screen could not be shared'))

    # drawing

    def _draw(self, x, y):
        if not _local_on_screen():
            return
        info = screen_capture_info()
        why = []
        screen = _shared_qscreen(info, why)
        if screen is None:
            log('no screen to draw the pointer on')
            return
        decision = (screen.name(), tuple(info.get('position') or ()), tuple(info.get('size') or ()), tuple(info.get('source_size') or ()))
        if decision != self.pointer_screen_logged:
            self.pointer_screen_logged = decision
            log('pointers go to screen %s (by %s); the desktop shares position=%s size=%s, captured %s; screens: %s' % (
                screen.name(), why[-1] if why else '?', info.get('position'), info.get('size'), info.get('source_size'), _describe_screens()))
        frame_w, frame_h = -1, -1
        try:
            frame_w, frame_h = SIPApplication.video_device.producer.framesize
        except Exception:
            pass
        source = info.get('source_size') or info.get('size')
        if source is None:
            geometry = screen.geometry()
            source = (geometry.width(), geometry.height())
        point = SP.frame_to_screen(x, y, frame_w, frame_h, source[0], source[1], letterboxed=frame_w > 0 and frame_h > 0)
        if point is None:
            return      # on the letterbox bars
        if self.overlay is None:
            self.overlay = PointerOverlay()
        self.overlay.show_marker(screen, point[0], point[1])
