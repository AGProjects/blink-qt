"""Radio push-to-talk in conference rooms (sylkserver docs/radio-ptt.md).

A conference participant sitting next to a two-way radio advertises that it can key it; the room
publishes the on-air state of that radio in conference-info, on the participant's endpoint, as
<agp-conf:radio_ptt state=... by=... elapsed=... timeout=... reason=.../>. Any other participant
can ask the room to press or release that radio, with a JSON message on the MSRP chat stream.

This module holds what does not belong to a window: the conference-info element (registered with
the sipsimple parser on import, or it is silently dropped), the radio state of a participant, the
press/release command and the walkie-talkie pictures.
"""

import json
import uuid

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QRadialGradient

from sipsimple.payloads import XMLAttribute, XMLElement
from sipsimple.payloads.conference import ConferenceDocument, Endpoint, EndpointExtension, agp_conf_namespace


__all__ = ('CONTENT_TYPE', 'RadioPTT', 'RadioEndpoint', 'radio_endpoints', 'combined_state', 'command', 'radio_pixmap', 'radio_icon', 'pulse')


CONTENT_TYPE = 'application/vnd.agp.radio-ptt+json'

STATES = ('idle', 'keying', 'transmitting', 'error')
KEYED_STATES = ('keying', 'transmitting')

# when a participant has several radio endpoints, the most active state is shown
_priority = {'transmitting': 3, 'keying': 2, 'error': 1, 'idle': 0}


class RadioPTT(XMLElement, EndpointExtension):
    """On-air state of the radio driven by a conference endpoint; its presence is the capability.
    The same definition as the room's, all attributes strings on the wire."""
    _xml_tag = 'radio_ptt'
    _xml_namespace = agp_conf_namespace
    _xml_document = ConferenceDocument

    state = XMLAttribute('state', type=str, required=True, test_equal=False)
    by = XMLAttribute('by', type=str, required=False, test_equal=False)
    elapsed = XMLAttribute('elapsed', type=str, required=False, test_equal=False)
    timeout = XMLAttribute('timeout', type=str, required=False, test_equal=False)
    reason = XMLAttribute('reason', type=str, required=False, test_equal=False)

    def __init__(self, state, by=None, elapsed=None, timeout=None, reason=None):
        XMLElement.__init__(self)
        self.state = state
        self.by = by
        self.elapsed = elapsed
        self.timeout = timeout
        self.reason = reason


if not hasattr(Endpoint, 'radio_ptt'):     # another module of the process may have registered it first
    Endpoint.register_extension('radio_ptt', RadioPTT)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class RadioEndpoint(object):
    """The radio of one endpoint (device) of a participant, as the last conference-info told it."""

    __slots__ = ('entity', 'state', 'by', 'elapsed', 'timeout', 'reason')

    def __init__(self, entity, state, by=None, elapsed=None, timeout=None, reason=None):
        self.entity = entity
        self.state = state if state in STATES else 'idle'
        self.by = by
        self.elapsed = _int(elapsed)
        self.timeout = _int(timeout)
        self.reason = reason

    @property
    def keyed(self):
        return self.state in KEYED_STATES

    @property
    def key(self):
        """What a change of it is judged by: elapsed changes on every NOTIFY and is no change."""
        return self.entity, self.state, self.by, self.timeout, self.reason

    def __repr__(self):
        return '%s(%r, %r, by=%r)' % (self.__class__.__name__, self.entity, self.state, self.by)


def radio_endpoints(user):
    """The radios of a conference-info <user>: its endpoints carrying <agp-conf:radio_ptt> that
    also have audio (the room cannot talk through one without), in document order."""
    radios = []
    for endpoint in user:
        element = getattr(endpoint, 'radio_ptt', None)
        if element is None or endpoint.entity is None:
            continue
        if not any(media.media_type is not None and media.media_type.value == 'audio' for media in endpoint):
            continue
        radios.append(RadioEndpoint(str(endpoint.entity), element.state, element.by, element.elapsed, element.timeout, element.reason))
    return radios


def combined_state(radios):
    """The state shown for several radios: the most active one; None for none."""
    if not radios:
        return None
    return max((radio.state for radio in radios), key=lambda state: _priority.get(state, 0))


def command(action, endpoint):
    """(id, body) of a press or release of the radio of an endpoint (its entity), to the room."""
    if action not in ('press', 'release'):
        raise ValueError('invalid radio PTT action: %r' % action)
    command_id = str(uuid.uuid4())
    return command_id, json.dumps({'action': action, 'id': command_id, 'endpoint': endpoint})


# The pulse: while a radio is on air its picture alternates every 0.5 s between the plain one and
# the one glowing red. One phase for the whole process, so the toolbar and the participants agree;
# the MSRP sessions window runs the timer that flips it.

class _Pulse(object):
    interval = 500     # ms
    on = False         # True: draw the glowing picture now


pulse = _Pulse()


# The pictures: icons/radio_ptt.png and icons/radio_ptt_tx.png; when
# they are not installed, a handheld radio drawn here, the 'tx' one with a red glow and its LED lit.

_cache = {}


def _draw_radio(painter, rect, tx, pad=0.0):
    w, h = rect.width(), rect.height()
    x0, y0 = rect.x(), rect.y()

    def r(x, y, rw, rh):
        return QRectF(x0 + x * w, y0 + y * h, rw * w, rh * h)

    if tx:
        # on air: the radio on a solid red tile filling the whole picture, with a thick white border
        # around the radio so it stands out of the red
        tile = rect.adjusted(-pad, -pad, pad, pad)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#e8101a'))
        painter.drawRoundedRect(tile, tile.width() * 0.22, tile.width() * 0.22)
        white = QColor('#ffffff')
        painter.setPen(QPen(white, max(2.0, w * 0.14), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(white)
        painter.drawRoundedRect(r(0.22, 0.02, 0.13, 0.30), w * 0.05, w * 0.05)
        painter.drawRoundedRect(r(0.60, 0.20, 0.16, 0.12), w * 0.03, w * 0.03)
        painter.drawRoundedRect(r(0.12, 0.29, 0.76, 0.69), w * 0.14, w * 0.14)
        painter.drawRoundedRect(r(0.04, 0.46, 0.10, 0.18), w * 0.03, w * 0.03)

    outline = QColor('#1e1e1e')
    body = QColor('#55595f')
    panel = QColor('#2b2d31')
    pen = QPen(outline, max(1.0, w * 0.035))

    # antenna, on the left of the top
    painter.setPen(pen)
    painter.setBrush(panel)
    painter.drawRoundedRect(r(0.22, 0.02, 0.13, 0.30), w * 0.05, w * 0.05)
    # knob, on the right of the top
    painter.drawRoundedRect(r(0.60, 0.20, 0.16, 0.12), w * 0.03, w * 0.03)
    # body
    painter.setBrush(body)
    painter.drawRoundedRect(r(0.12, 0.29, 0.76, 0.69), w * 0.14, w * 0.14)
    # PTT key on the left side
    painter.setBrush(panel)
    painter.drawRoundedRect(r(0.04, 0.46, 0.10, 0.18), w * 0.03, w * 0.03)
    # display
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('#9fc7d8'))
    painter.drawRoundedRect(r(0.24, 0.36, 0.52, 0.12), w * 0.03, w * 0.03)
    # speaker grille
    grille = QPen(panel, max(1.0, h * 0.022), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(grille)
    for i in range(4):
        y = y0 + (0.58 + i * 0.09) * h
        painter.drawLine(QPointF(x0 + 0.27 * w, y), QPointF(x0 + 0.73 * w, y))
    # LED on top of the body
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('#ff2a2a') if tx else QColor('#5c2020'))
    painter.drawEllipse(QPointF(x0 + 0.50 * w, y0 + 0.325 * h), w * 0.05, w * 0.05)


def _bounds(image):
    """The rectangle (x, y, w, h) of what is drawn in an image, None when nothing is."""
    width, height = image.width(), image.height()
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            if (image.pixel(x, y) >> 24) > 16:
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
    return None if right < left else (left, top, right, bottom)


def _image(tx):
    """The picture of the radio from resources (icons/radio_ptt.png, and icons/radio_ptt_tx.png with
    the red glow), without the transparent margins of the square picture (the radio is tall and
    narrow); both cut the same, so the radio keeps its size while it pulses. None when not installed."""
    if 'images' not in _cache:
        import os
        from PyQt6.QtGui import QImage
        from blink.resources import Resources
        images = {}
        for state, name in ((False, 'icons/radio_ptt.png'), (True, 'icons/radio_ptt_tx.png')):
            try:
                path = Resources.get(name)
            except Exception:
                path = None
            if path and os.path.isfile(path):
                image = QImage(path)
                if not image.isNull():
                    images[state] = image.convertToFormat(QImage.Format.Format_ARGB32)
        bounds = [_bounds(image) for image in images.values()]
        bounds = [b for b in bounds if b is not None]
        if bounds and len({(image.width(), image.height()) for image in images.values()}) == 1:
            left, top = min(b[0] for b in bounds), min(b[1] for b in bounds)
            right, bottom = max(b[2] for b in bounds), max(b[3] for b in bounds)
            images = {state: image.copy(left, top, right - left + 1, bottom - top + 1) for state, image in images.items()}
        _cache['images'] = images
    return _cache['images'].get(bool(tx))


def radio_pixmap(height, tx=False, ratio=1.0):
    """The radio picture, height logical pixels high (its width as the picture's proportions give)."""
    height = max(int(height), 8)
    key = (height, bool(tx), round(float(ratio), 2))
    pixmap = _cache.get(key)
    if pixmap is None:
        image = _image(tx) or (_image(False) if tx else None)
        if image is not None:
            pixmap = QPixmap.fromImage(image.scaledToHeight(int(round(height * ratio)), Qt.TransformationMode.SmoothTransformation))
            pixmap.setDevicePixelRatio(ratio)
            _cache[key] = pixmap
            return pixmap
        width = max(int(round(height * 0.72)), 6)
        pixmap = QPixmap(int(round(width * ratio)), int(round(height * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pad = max(2.0, height * 0.12)     # room for the on-air tile, the same in both, so the radio keeps its size
        _draw_radio(painter, QRectF(pad, pad, width - 2 * pad, height - 2 * pad), tx, pad)
        painter.end()
        _cache[key] = pixmap
    return pixmap


def radio_icon(size, tx=False, ratio=1.0):
    """The radio picture centred in a square icon (the toolbar button)."""
    key = ('icon', int(size), bool(tx), round(float(ratio), 2))
    icon = _cache.get(key)
    if icon is None:
        canvas = QPixmap(int(round(size * ratio)), int(round(size * ratio)))
        canvas.setDevicePixelRatio(ratio)
        canvas.fill(Qt.GlobalColor.transparent)
        picture = radio_pixmap(size, tx, ratio)
        painter = QPainter(canvas)
        width = picture.width() / ratio
        painter.drawPixmap(QPointF((size - width) / 2, 0), picture)
        painter.end()
        icon = _cache[key] = QIcon(canvas)
    return icon
