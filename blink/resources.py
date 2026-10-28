
"""Provide access to Blink's resources"""

import __main__
import os
import platform
import sys
import pathlib

from PyQt6.QtCore import Qt, QBuffer
from PyQt6.QtGui import QIcon, QPixmap

from application.python.descriptor import classproperty
from application.python.types import Singleton
from application.system import makedirs, unlink

from sipsimple.configuration.datatypes import Path
from blink.util import run_in_gui_thread


__all__ = ['ApplicationData', 'Resources', 'IconManager', 'themed_icon', 'lightness_inverted_icon', 'ui_settings']


def themed_icon(filename, dark_color='#b4b4b4', size=64, outline=False):
    """QIcon of a dark monochrome glyph (the default avatar, the call buttons), redrawn light
    under a dark theme where the original disappears into the background. A vector image is
    drawn at `size` pixels first, so the light copy stays sharp at any button size.

    outline: the glyph is dark lines on a light fill (a clock face): only the dark parts are
    redrawn light, the light fill becomes transparent, instead of the whole shape becoming one blot."""
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QColor, QPainter, QPalette
    from PyQt6.QtWidgets import QApplication
    application = QApplication.instance()
    if application is None or application.palette().color(QPalette.ColorRole.Window).lightness() >= 128:
        return QIcon(filename)
    pixmap = QIcon(filename).pixmap(QSize(size, size)) if filename.endswith('.svg') else QPixmap(filename)
    if pixmap.isNull():
        return QIcon(filename)
    if outline:
        from PyQt6.QtGui import QImage
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        light = QColor(dark_color)
        for y in range(image.height()):
            for x in range(image.width()):
                pixel = image.pixelColor(x, y)
                if pixel.alpha() == 0:
                    continue
                darkness = 1.0 - pixel.valueF()
                image.setPixelColor(x, y, QColor(light.red(), light.green(), light.blue(), round(pixel.alpha() * darkness)))
        return QIcon(QPixmap.fromImage(image))
    painter = QPainter(pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), QColor(dark_color))
    painter.end()
    return QIcon(pixmap)

def lightness_inverted_icon(filename):
    """QIcon of a shaded, mostly dark picture (the Preferences toolbar icons), with its lightness
    inverted under a dark theme: hue, saturation and the shading detail stay, the dark body
    turns light. Unlike themed_icon, which flattens a glyph into one colour."""
    from PyQt6.QtGui import QColor, QImage, QPalette
    from PyQt6.QtWidgets import QApplication
    application = QApplication.instance()
    if application is None or application.palette().color(QPalette.ColorRole.Window).lightness() >= 128:
        return QIcon(filename)
    image = QImage(filename)
    if image.isNull():
        return QIcon(filename)
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() == 0:
                continue
            hue, saturation, lightness, alpha = pixel.getHslF()
            image.setPixelColor(x, y, QColor.fromHslF(hue, saturation, 1.0 - lightness * 0.85, alpha))
    return QIcon(QPixmap.fromImage(image))


class DirectoryContextManager(str):
    def __enter__(self):
        self.directory = os.getcwd()
        os.chdir(self)

    def __exit__(self, type, value, traceback):
        os.chdir(self.directory)


class ApplicationData(object):
    """Provide access to user data"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
            if platform.system() == 'Darwin':
                from Foundation import NSApplicationSupportDirectory, NSSearchPathForDirectoriesInDomains, NSUserDomainMask
                cls._cached_directory = os.path.join(NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0], 'Blink-Qt')
            elif platform.system() == 'Windows':
                cls._cached_directory = os.path.join(os.environ['APPDATA'], 'Blink')
            else:
                cls._cached_directory = Path('~/.blink').normalized
        return DirectoryContextManager(cls._cached_directory)

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, os.path.normpath(resource))


_ui_settings_migrated = False


def ui_settings():
    """Where the windows keep their state (geometry, splitters, the chat window's tiles, the
    message pane's text size): <ApplicationData>/ui.conf, an INI file with the rest of Blink's
    configuration and logs, not the Qt default (~/.config/AG Projects/Blink.conf). Use it instead
    of QSettings(). The first time, what the Qt default place held is copied over."""
    global _ui_settings_migrated
    from PyQt6.QtCore import QSettings
    path = ApplicationData.get('ui.conf')
    if not _ui_settings_migrated:
        _ui_settings_migrated = True
        if not os.path.exists(path):
            old = QSettings()       # the Qt default place, any format
            keys = old.allKeys()
            if keys:
                new = QSettings(path, QSettings.Format.IniFormat)
                for key in keys:
                    new.setValue(key, old.value(key))
                new.sync()
                from blink.logging import ActivityLog
                ActivityLog().info(f'[ui] Window state moved from {old.fileName()} to {path} ({len(keys)} keys)')
    return QSettings(path, QSettings.Format.IniFormat)


class Resources(object):
    """Provide access to Blink's resources"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
            parent_dir = pathlib.Path(__file__).parent.absolute()
            try:
                binary_directory = os.path.dirname(os.path.realpath(__main__.__file__))
            except AttributeError:
                if hasattr(sys, 'frozen'):
                    application_directory = os.path.dirname(os.path.realpath(sys.executable))
                else:
                    application_directory = os.path.realpath('')  # executed in interactive interpreter
            else:
                if os.path.basename(binary_directory) == 'bin':
                    application_directory = os.path.dirname(binary_directory)
                else:
                    application_directory = binary_directory
            unit_test_dir = '%s/../../../../resources' % parent_dir
            if os.path.exists(os.path.join(unit_test_dir, 'blink.ui')):
                # this is to make happy the self unit-testing, there must be a better way -adi
                cls._cached_directory = unit_test_dir
            elif os.path.exists(os.path.join(application_directory, 'resources', 'blink.ui')):
                cls._cached_directory = os.path.join(application_directory, 'resources')
            else:
                cls._cached_directory = os.path.join(application_directory, 'share', 'blink')
        return DirectoryContextManager(cls._cached_directory)

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, os.path.normpath(resource))


class IconManager(object, metaclass=Singleton):
    max_size = 256

    def __init__(self):
        self.iconmap = {}

    @run_in_gui_thread(wait=True)
    def get(self, id):
        id = id.replace('/', '_')
        try:
            return self.iconmap[id]
        except KeyError:
            pixmap = QPixmap()
            filename = ApplicationData.get(os.path.join('images', id + '.png'))
            try:
                with open(filename, 'rb') as f:
                    data = f.read()
            except (IOError, OSError):
                data = None
            if data is not None and pixmap.loadFromData(data):
                icon = QIcon(pixmap)
                icon.filename = filename
                icon.content = data
                icon.content_type = 'image/png'
            else:
                icon = None
            return self.iconmap.setdefault(id, icon)

    @run_in_gui_thread(wait=True)
    def store_data(self, id, data):
        data = data if isinstance(data, bytes) else data.encode()
        id = id.replace('/', '_')
        directory = ApplicationData.get('images')
        filename = os.path.join(directory, id + '.png')
        makedirs(directory)
        pixmap = QPixmap()
        if data is not None and pixmap.loadFromData(data):
            image_size = pixmap.size()
            if image_size.width() > self.max_size or image_size.height() > self.max_size:
                pixmap = pixmap.scaled(self.max_size, self.max_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            if not data.startswith(b'\x89PNG\r\n\x1a\n') or pixmap.size() != image_size:
                buffer = QBuffer()
                pixmap.save(buffer, 'png')
                data = bytes(buffer.data())
            with open(filename, 'wb') as f:
                data = data if isinstance(data, bytes) else data.encode()
                f.write(data)
            icon = QIcon(pixmap)
            icon.filename = filename
            icon.content = data
            icon.content_type = 'image/png'
        else:
            unlink(filename)
            icon = None
        self.iconmap[id] = icon
        return icon

    @run_in_gui_thread(wait=True)
    def store_file(self, id, file):
        id = id.replace('/', '_')
        directory = ApplicationData.get('images')
        filename = os.path.join(directory, id + '.png')
        if filename == os.path.normpath(file):
            return self.iconmap.get(id, None)
        makedirs(directory)
        pixmap = QPixmap()
        if file is not None and pixmap.load(file):
            if pixmap.size().width() > self.max_size or pixmap.size().height() > self.max_size:
                pixmap = pixmap.scaled(self.max_size, self.max_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            buffer = QBuffer()
            pixmap.save(buffer, 'png')
            data = buffer.data()
            with open(filename, 'wb') as f:
                f.write(data)
            icon = QIcon(pixmap)
            icon.filename = filename
            icon.content = data
            icon.content_type = 'image/png'
        else:
            unlink(filename)
            icon = None
        self.iconmap[id] = icon
        return icon

    @run_in_gui_thread(wait=True)
    def remove(self, id):
        id = id.replace('/', '_')
        self.iconmap.pop(id, None)
        unlink(ApplicationData.get(os.path.join('images', id + '.png')))




# Sharp pictures on scaled screens
#
# A QPixmap loaded from a file has the file's pixels at a device pixel ratio of 1: on a screen
# scaled 2x it is stretched, blurred or blocky. An SVG is drawn instead at the size it is shown
# times the largest ratio of the screens (scaled down where the ratio is lower).

def device_pixel_ratio():
    """The largest device pixel ratio of the screens."""
    from PyQt6.QtGui import QGuiApplication
    screens = QGuiApplication.screens() if QGuiApplication.instance() is not None else []
    return max([screen.devicePixelRatio() for screen in screens] or [1.0])


def sharp_pixmap(filename, width=None, height=None):
    """The picture of filename (an SVG drawn at the device pixel ratio) at width x height logical
    pixels, by default the size it is drawn at. Other formats are loaded as they are."""
    from PyQt6.QtCore import QSize
    if not filename.endswith('.svg'):
        return QPixmap(filename)
    icon = QIcon(filename)
    if width is None or height is None:
        from PyQt6.QtGui import QImageReader
        size = QImageReader(filename).size()            # the SVG's own size
        if not size.isValid():
            return QPixmap(filename)
        width, height = width or size.width(), height or size.height()
    return icon.pixmap(QSize(width, height), device_pixel_ratio())


_ui_pixmaps = {}


def _label_pixmaps(ui_file):
    """{widget name: the pixmap file} of the labels of a .ui file that show an SVG."""
    if ui_file not in _ui_pixmaps:
        from xml.etree import ElementTree
        found = {}
        try:
            root = ElementTree.parse(ui_file).getroot()
        except (OSError, ElementTree.ParseError):
            root = None
        for widget in (root.iter('widget') if root is not None else ()):
            for prop in widget.findall('property'):
                pixmap = prop.find('pixmap')
                if prop.get('name') == 'pixmap' and pixmap is not None and (pixmap.text or '').strip().endswith('.svg'):
                    found[widget.get('name')] = pixmap.text.strip()
        _ui_pixmaps[ui_file] = found
    return _ui_pixmaps[ui_file]


def sharpen_ui_pixmaps(ui, ui_file):
    """The SVG pictures of a form's labels drawn again at the device pixel ratio, at the size uic gave them."""
    from PyQt6.QtWidgets import QLabel
    ratio = device_pixel_ratio()
    if ratio <= 1.0:
        return
    for name, filename in _label_pixmaps(ui_file).items():
        label = getattr(ui, name, None)
        if not isinstance(label, QLabel):
            continue
        current = label.pixmap()
        if current is None or current.isNull() or current.devicePixelRatio() >= ratio:
            continue
        size = current.deviceIndependentSize()
        pixmap = sharp_pixmap(Resources.get(filename), round(size.width()), round(size.height()))
        if not pixmap.isNull():
            label.setPixmap(pixmap)


def load_ui_type(name):
    """uic.loadUiType() of a resource .ui file, its setupUi() also drawing the labels' SVG pictures sharp."""
    from PyQt6 import uic
    ui_file = Resources.get(name)
    ui_class, base_class = uic.loadUiType(ui_file)
    setup = ui_class.setupUi

    def setupUi(self, *args, **kw):
        setup(self, *args, **kw)
        try:
            sharpen_ui_pixmaps(self, ui_file)
        except Exception as e:
            from blink.logging import ActivityLog
            ActivityLog().warning(f'[ui] Cannot redraw the pictures of {name}: {e}')

    ui_class.setupUi = setupUi
    return ui_class, base_class
