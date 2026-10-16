"""Sharp pictures on scaled (HiDPI) screens.

Pixmaps made from SVG files at their intrinsic size (what QPixmap(file) and uic's <pixmap>
properties do) are 12 or 16 device pixels, stretched and blurred at a scale above 100%.
Widgets drawn into list views with grab() have the same problem when they were never shown.
These helpers render at the screen's device pixel ratio instead.
"""

import xml.etree.ElementTree as ElementTree

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QGuiApplication, QIcon, QPixmap
from PyQt6.QtWidgets import QLabel

from blink.resources import Resources


__all__ = ['screen_ratio', 'svg_pixmap', 'sharpen_ui_pixmaps', 'render_widget']


def screen_ratio():
    """The highest device pixel ratio of the screens (what a window may be moved to)."""
    try:
        return max([screen.devicePixelRatio() for screen in QGuiApplication.screens()] or [1.0])
    except Exception:
        return 1.0


def svg_pixmap(path, size=None, ratio=None):
    """A pixmap of an SVG (or any icon file) at size (logical pixels; the file's own size if None),
    rendered for ratio (the screens' if None)."""
    if size is None:
        size = QPixmap(path).size()
        if size.isEmpty():
            size = QSize(16, 16)
    elif isinstance(size, int):
        size = QSize(size, size)
    return QIcon(path).pixmap(size, ratio or screen_ratio())


def sharpen_ui_pixmaps(widget, ui_name):
    """Render again, at the screen's ratio, the SVG <pixmap> properties of the QLabels that the
    .ui file ui_name put on widget (uic loads them as pixmaps at their intrinsic size)."""
    try:
        tree = ElementTree.parse(Resources.get(ui_name))
    except (OSError, ElementTree.ParseError):
        return
    ratio = screen_ratio()
    for element in tree.iter('widget'):
        name = element.get('name')
        for prop in element.findall('property'):
            if prop.get('name') != 'pixmap':
                continue
            node = prop.find('pixmap')
            if node is None or not (node.text or '').strip().lower().endswith('.svg'):
                continue
            label = getattr(widget, name, None)
            if not isinstance(label, QLabel):
                continue
            current = label.pixmap()
            size = current.deviceIndependentSize().toSize() if current is not None and not current.isNull() else None
            label.setPixmap(svg_pixmap(Resources.get(node.text.strip()), size, ratio))


def render_widget(widget, size, ratio):
    """widget drawn into a transparent pixmap of size (logical pixels) at ratio: what grab() gives,
    but sharp on a scaled screen whether or not the widget was ever shown."""
    pixmap = QPixmap(max(1, round(size.width() * ratio)), max(1, round(size.height() * ratio)))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    widget.render(pixmap)
    return pixmap
