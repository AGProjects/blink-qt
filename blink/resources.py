
"""Provide access to Blink's resources"""

import __main__
import imghdr
import os
import platform
import sys

from PyQt4.QtCore import Qt, QBuffer
from PyQt4.QtGui  import QIcon, QPixmap

from application.python.descriptor import classproperty
from application.python.types import Singleton
from application.system import makedirs, unlink

from sipsimple.configuration.datatypes import Path
from blink.util import run_in_gui_thread


__all__ = ['ApplicationData', 'Resources', 'IconManager']


class DirectoryContextManager(unicode):
    def __enter__(self):
        self.directory = os.getcwdu()
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
                cls._cached_directory = os.path.join(NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0], u'Blink')
            elif platform.system() == 'Windows':
                cls._cached_directory = os.path.join(os.environ['APPDATA'].decode(sys.getfilesystemencoding()), u'Blink')
            else:
                cls._cached_directory = Path(u'~/.blink').normalized
        return DirectoryContextManager(cls._cached_directory)

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, os.path.normpath(resource))


class Resources(object):
    """Provide access to Blink's resources"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
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
            if os.path.exists(os.path.join(application_directory, 'resources', 'blink.ui')):
                cls._cached_directory = os.path.join(application_directory, 'resources').decode(sys.getfilesystemencoding())
            else:
                cls._cached_directory = os.path.join(application_directory, 'share', 'blink').decode(sys.getfilesystemencoding())
        return DirectoryContextManager(cls._cached_directory)

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, os.path.normpath(resource))


class IconManager(object):
    __metaclass__ = Singleton

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
        id = id.replace('/', '_')
        directory = ApplicationData.get('images')
        filename = os.path.join(directory, id + '.png')
        makedirs(directory)
        pixmap = QPixmap()
        if data is not None and pixmap.loadFromData(data):
            image_size = pixmap.size()
            if image_size.width() > self.max_size or image_size.height() > self.max_size:
                pixmap = pixmap.scaled(self.max_size, self.max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if imghdr.what(None, data) != 'png' or pixmap.size() != image_size:
                buffer = QBuffer()
                pixmap.save(buffer, 'png')
                data = str(buffer.data())
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
                pixmap = pixmap.scaled(self.max_size, self.max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            buffer = QBuffer()
            pixmap.save(buffer, 'png')
            data = str(buffer.data())
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


