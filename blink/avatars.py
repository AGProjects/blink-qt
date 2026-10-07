
"""Generated avatars for contacts without a picture.

Same treatment as Blink for macOS (Avatars.py) and Sylk Mobile: two initials
on a colour derived from the contact's name, so a photoless contact looks the
same on every client instead of wearing the shared grey person glyph.
"""

import os
import re
import zlib

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap

from application.system import makedirs

from blink.resources import ApplicationData


__all__ = ['AVATAR_PALETTE', 'avatar_initials', 'avatar_color', 'avatar_pixmap', 'avatar_icon', 'circular_icon']


# Same palette and the same hash as macOS, so a contact keeps its colour across clients.
AVATAR_PALETTE = (
    (0x5B, 0x8C, 0xC4), (0x6A, 0xB0, 0x7A), (0xC4, 0x8B, 0x5B),
    (0xA5, 0x7A, 0xC4), (0xC4, 0x5B, 0x6E), (0x4F, 0xA8, 0xA8),
    (0xC4, 0xA8, 0x4F), (0x7A, 0x8C, 0xA5),
)

_initials_re = re.compile(r'[^0-9A-Za-z]+')
_phone_re = re.compile(r'^\+?[0-9(][0-9\s().-]*$')

_icon_sizes = (16, 24, 32, 48, 64, 128)
_icon_cache = {}


def avatar_initials(name):
    """Up to two initials for a display name or SIP address.

    'Alice Smith' -> AS, 'bob@example.com' -> BO, 'sip:jan.de.vries@x' -> JD, '+31 20 123 4567' -> 67
    """
    if not name:
        return '?'
    text = str(name).strip()
    for scheme in ('sips:', 'sip:'):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    if '@' in text and ' ' not in text:
        text = text.split('@', 1)[0]
    if _phone_re.match(text):
        digits = re.sub(r'\D', '', text)
        return digits[-2:] if len(digits) >= 2 else (digits or '?')
    tokens = [t for t in _initials_re.split(text) if t]
    if not tokens:
        return '?'
    if len(tokens) == 1:
        return tokens[0][:2].upper()
    return (tokens[0][0] + tokens[1][0]).upper()


def _palette_index(name):
    key = str(name or '').strip().lower().encode('utf-8')
    return zlib.crc32(key) % len(AVATAR_PALETTE)


def avatar_color(name):
    return QColor(*AVATAR_PALETTE[_palette_index(name)])


def _draw(size, initials, color):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    rect = QRectF(0, 0, size, size)
    path = QPainterPath()
    path.addEllipse(rect)
    painter.fillPath(path, color)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(round(size * 0.42), 6))
    painter.setFont(font)
    painter.setPen(QColor('white'))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, initials)
    painter.end()
    return pixmap


def avatar_pixmap(name, size=32):
    return _draw(size, avatar_initials(name), avatar_color(name))


def avatar_icon(name):
    """A QIcon with the initials avatar for name, drawn at several sizes so the letters stay sharp.

    The icon carries a filename (like the other contact icons) pointing to a PNG copy, for the
    places that need an image on disk (the MSRP chat transcript)."""
    initials = avatar_initials(name)
    index = _palette_index(name)
    key = (initials, index)
    try:
        return _icon_cache[key]
    except KeyError:
        pass
    color = QColor(*AVATAR_PALETTE[index])
    icon = QIcon()
    for size in _icon_sizes:
        icon.addPixmap(_draw(size, initials, color))
    directory = ApplicationData.get('images/generated')
    filename = os.path.join(directory, 'avatar-%s-%d.png' % (initials, index))
    if not os.path.exists(filename):
        try:
            makedirs(directory)
            _draw(128, initials, color).save(filename, 'PNG')
        except Exception:
            filename = None
    icon.filename = filename
    icon.generated = True
    return _icon_cache.setdefault(key, icon)


def _circular(source, size):
    """source aspect-filled into a circle of size pixels, the overflow cropped (as on macOS)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    if source.isNull() or not source.width() or not source.height():
        return pixmap
    scale = max(size / source.width(), size / source.height())
    width = source.width() * scale
    height = source.height() * scale
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, size, size))
    painter.setClipPath(path)
    painter.drawPixmap(QRectF((size - width) / 2, (size - height) / 2, width, height), source, QRectF(source.rect()))
    painter.end()
    return pixmap


def circular_icon(icon):
    """A photograph as a circle. Keeps the filename the photo icon carries."""
    if icon is None or getattr(icon, 'generated', False):
        return icon
    sizes = icon.availableSizes()
    largest = max(sizes, key=lambda s: s.width() * s.height()) if sizes else None
    source = icon.pixmap(largest) if largest is not None else icon.pixmap(128)
    if source.isNull():
        return icon
    result = QIcon()
    for size in _icon_sizes:
        result.addPixmap(_circular(source, size))
    result.filename = getattr(icon, 'filename', None)
    for attribute in ('content', 'content_type'):
        if hasattr(icon, attribute):
            setattr(result, attribute, getattr(icon, attribute))
    result.generated = True
    return result
