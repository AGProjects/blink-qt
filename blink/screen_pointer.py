"""Screen sharing in calls -- wire format and geometry.

Interoperates with sylk-mobile (app/components/CallCapabilities.js,
VideoBox.js _sendPointer, the in-call message dispatch in app.js). Every
message is an in-dialog SIP MESSAGE on the call, JSON bodies:

  application/sylk-capabilities       {"version": 1, "capabilities": [...]}
      Sent once by each side when the call starts. A peer that never sends
      one supports nothing; unknown tokens are ignored. "pointer" means the
      sender draws pointers it receives on the screen it shares.

  application/sylk-screen-sharing     {"action": "start" | "stop"}
      The sender started/stopped sending its screen as video.

                                      {"action": "request", "id": <uuid>,
                                       "expires": <ISO 8601>}
      "Please share your screen". Offered only to a peer that advertised
      "screen-sharing" (it can send a screen) and "screen-request" (it
      understands this handshake). A request that arrives expired is dropped
      without a reply; one left unanswered expires silently on both sides.

                                      {"action": "request_accept" | "request_reject",
                                       "id": <uuid>}
      The answer. An accept is followed by an ordinary "start" once the
      screen is actually on the wire; a peer already sharing accepts at once.

  application/sylk-pointer            {"x": 0..1, "y": 0..1, "t": <ms>}
      The viewer clicked the shared screen. x/y are normalized on the video
      frame, origin top-left. t identifies the click.

  application/sylk-pointer-ack        {"t": <ms>}
      The sharer drew click t; the viewer echoes it locally.

  application/sylk-pointer-visibility {"inApp": true | false}
      The sharer can (not) draw pointers right now (iOS can only draw inside
      Sylk). Blink draws over the whole screen, so it never sends this.

Nothing here touches Qt or the SIP SIMPLE SDK. screen_share.py does the
notifications, sending and drawing.
"""

import datetime
import json
import re
import time
import uuid

__all__ = ['CAPABILITIES_CONTENT_TYPE', 'SCREEN_SHARING_CONTENT_TYPE',
           'POINTER_CONTENT_TYPE', 'POINTER_ACK_CONTENT_TYPE',
           'POINTER_VISIBILITY_CONTENT_TYPE', 'CONTENT_TYPES',
           'CAP_POINTER', 'CAP_SCREEN_SHARING', 'CAP_SCREEN_REQUEST', 'REQUEST_TTL',
           'SCREEN_DEVICE_NAME', 'my_capabilities', 'build_capabilities', 'parse_capabilities',
           'build_sharing', 'parse_sharing', 'parse_sharing_signal',
           'build_request', 'build_request_reply', 'parse_expires',
           'build_pointer', 'parse_pointer', 'build_ack', 'parse_ack', 'parse_visibility',
           'new_click_id', 'screen_index', 'is_screen_device',
           'fit_rect', 'view_to_frame', 'frame_to_view', 'frame_to_screen']


CAPABILITIES_CONTENT_TYPE = 'application/sylk-capabilities'
SCREEN_SHARING_CONTENT_TYPE = 'application/sylk-screen-sharing'
POINTER_CONTENT_TYPE = 'application/sylk-pointer'
POINTER_ACK_CONTENT_TYPE = 'application/sylk-pointer-ack'
POINTER_VISIBILITY_CONTENT_TYPE = 'application/sylk-pointer-visibility'

CONTENT_TYPES = frozenset([CAPABILITIES_CONTENT_TYPE, SCREEN_SHARING_CONTENT_TYPE,
                           POINTER_CONTENT_TYPE, POINTER_ACK_CONTENT_TYPE,
                           POINTER_VISIBILITY_CONTENT_TYPE])

CAPABILITIES_VERSION = 1

# Wire tokens, keep them identical to sylk-mobile's CallCapabilities.js.
CAP_SCREEN_SHARING = 'screen-sharing'
CAP_SCREEN_REQUEST = 'screen-request'
CAP_POINTER = 'pointer'

# Lifetime of a screen request, as sylk-mobile puts on the wire.
REQUEST_TTL = 60

# The capture device python3-sipsimple lists for the screen: "My screen" (the
# monitor picked in the desktop's dialog on Linux). Where every display is a
# device of its own, the next ones are "My screen 2", "My screen 3"...
SCREEN_DEVICE_NAME = 'My screen'

_screen_name_re = re.compile(r'^%s(?: (\d+))?$' % re.escape(SCREEN_DEVICE_NAME))


def my_capabilities(can_share_screen=True):
    """Only claim screen sharing when the screen can be captured here (the
    "My screen" device exists): the peer offers "Request screen" on it."""
    if can_share_screen:
        return [CAP_SCREEN_SHARING, CAP_SCREEN_REQUEST, CAP_POINTER]
    return [CAP_POINTER]


def _loads(content):
    if isinstance(content, (bytes, bytearray)):
        content = bytes(content).decode('utf-8')
    return json.loads(content)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# capabilities

def build_capabilities(capabilities=None):
    if capabilities is None:
        capabilities = my_capabilities()
    return json.dumps({'version': CAPABILITIES_VERSION, 'capabilities': list(capabilities)})


def parse_capabilities(content):
    """List of tokens, or None if the payload is unusable."""
    try:
        tokens = _loads(content).get('capabilities')
    except Exception:
        return None
    if not isinstance(tokens, list):
        return None
    return [token for token in tokens if isinstance(token, str)]


# screen sharing start / stop / request

SHARING_ACTIONS = ('start', 'stop', 'request', 'request_accept', 'request_reject')


def build_sharing(action):
    if action not in ('start', 'stop'):
        raise ValueError('action must be start or stop')
    return json.dumps({'action': action})


def parse_sharing_signal(content):
    """{'action': ..., 'id': ..., 'expires': ...} or None. The request actions
    need an id; 'expires' is left as sent (see parse_expires)."""
    try:
        data = _loads(content)
        action = data.get('action')
    except Exception:
        return None
    if action not in SHARING_ACTIONS:
        return None
    request_id = data.get('id')
    if action.startswith('request'):
        if not isinstance(request_id, str) or not request_id:
            return None
    else:
        request_id = None
    return {'action': action, 'id': request_id, 'expires': data.get('expires')}


def parse_sharing(content):
    """'start' or 'stop'; None for anything else."""
    signal = parse_sharing_signal(content)
    return signal['action'] if signal and signal['action'] in ('start', 'stop') else None


def _iso_utc(timestamp):
    moment = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (moment.microsecond // 1000)


def build_request(request_id=None, now=None):
    """(request_id, body). Expires REQUEST_TTL from now, in the same
    toISOString() form sylk-mobile writes."""
    if request_id is None:
        request_id = str(uuid.uuid4())
    if now is None:
        now = time.time()
    body = json.dumps({'action': 'request', 'id': request_id, 'expires': _iso_utc(now + REQUEST_TTL)})
    return request_id, body


def build_request_reply(action, request_id):
    if action not in ('request_accept', 'request_reject'):
        raise ValueError('action must be request_accept or request_reject')
    return json.dumps({'action': action, 'id': request_id})


def parse_expires(value):
    """Epoch seconds of an ISO 8601 timestamp (or of epoch milliseconds),
    None when unusable. A timestamp without a zone is taken as UTC."""
    if _number(value):
        return value / 1000.0
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    match = re.match(r'^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$', text)
    if match is not None and match.group(2):
        # fromisoformat before Python 3.11 wants 3 or 6 fraction digits
        fraction = (match.group(2)[1:] + '000000')[:6]
        text = '%s.%s%s' % (match.group(1), fraction, match.group(3))
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


# pointer

def new_click_id():
    """Milliseconds, like Date.now() on sylk-mobile."""
    return int(time.time() * 1000)


def build_pointer(x, y, t):
    return json.dumps({'x': round(x, 3), 'y': round(y, 3), 't': t})


def parse_pointer(content):
    """(x, y, t) with x/y in 0..1 and t possibly None, or None."""
    try:
        data = _loads(content)
        x, y = data.get('x'), data.get('y')
    except Exception:
        return None
    if not (_number(x) and _number(y)) or not (0 <= x <= 1 and 0 <= y <= 1):
        return None
    t = data.get('t')
    return x, y, (t if _number(t) else None)


def build_ack(t):
    return json.dumps({'t': t})


def parse_ack(content):
    try:
        t = _loads(content).get('t')
    except Exception:
        return None
    return t if _number(t) else None


def parse_visibility(content):
    try:
        in_app = _loads(content).get('inApp')
    except Exception:
        return None
    return in_app if isinstance(in_app, bool) else None


# capture devices

def screen_index(device_name):
    """0 for "My screen", n-1 for "My screen n", None for a camera."""
    match = _screen_name_re.match(device_name or '')
    if match is None:
        return None
    number = int(match.group(1)) if match.group(1) else 1
    return number - 1 if number >= 1 else None


def is_screen_device(device_name):
    return screen_index(device_name) is not None


# geometry
#
# Normalized coordinates are on the video FRAME, origin top-left, as the
# viewer measures them. View and screen coordinates here are Qt's, origin
# top-left as well.

def fit_rect(content_w, content_h, box_w, box_h):
    """(x, y, w, h) of content aspect-fitted and centred in the box."""
    if content_w <= 0 or content_h <= 0 or box_w <= 0 or box_h <= 0:
        return 0.0, 0.0, float(box_w), float(box_h)
    scale = min(float(box_w) / content_w, float(box_h) / content_h)
    w, h = content_w * scale, content_h * scale
    return (box_w - w) / 2.0, (box_h - h) / 2.0, w, h


def view_to_frame(px, py, view_w, view_h, frame_w, frame_h):
    """A click in a view showing the frame aspect-fitted -> normalized point
    on the frame, or None when the click is on the black bars."""
    x, y, w, h = fit_rect(frame_w, frame_h, view_w, view_h)
    if w <= 0 or h <= 0:
        return None
    nx = (px - x) / w
    ny = (py - y) / h
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        return None
    return nx, ny


def frame_to_view(nx, ny, view_w, view_h, frame_w, frame_h):
    x, y, w, h = fit_rect(frame_w, frame_h, view_w, view_h)
    return x + nx * w, y + ny * h


def frame_to_screen(nx, ny, frame_w, frame_h, screen_w, screen_h, letterboxed=True):
    """Point normalized on the frame we send -> normalized on the monitor.

    The capture letterboxes: the monitor sits aspect-fitted inside the
    frame, and a click on the bars points at nothing (None). Without
    letterboxing the monitor was scaled to the frame and the coordinates are
    the same.
    """
    if not letterboxed or frame_w <= 0 or frame_h <= 0 or screen_w <= 0 or screen_h <= 0:
        return nx, ny
    x, y, w, h = fit_rect(screen_w, screen_h, frame_w, frame_h)
    if w <= 0 or h <= 0:
        return None
    sx = (nx * frame_w - x) / w
    sy = (ny * frame_h - y) / h
    if not (0 <= sx <= 1 and 0 <= sy <= 1):
        return None
    return sx, sy
