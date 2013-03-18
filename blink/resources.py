# Copyright (C) 2010-2013 AG Projects. See LICENSE for details.
#

"""Provide access to Blink's resources"""

__all__ = ['ApplicationData', 'Resources', 'IconManager']

import os
import platform
import sys

from PyQt4.QtCore import Qt
from PyQt4.QtGui  import QIcon, QPixmap
from application.python.descriptor import classproperty
from application.python.types import Singleton
from application.system import makedirs, unlink


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
                cls._cached_directory = os.path.join(os.environ['APPDATA'], 'Blink').decode(sys.getfilesystemencoding())
            else:
                cls._cached_directory = os.path.expanduser('~/.blink').decode(sys.getfilesystemencoding())
        return DirectoryContextManager(cls._cached_directory)

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, resource or u'')


class Resources(object):
    """Provide access to Blink's resources"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
            script = sys.argv[0]
            if script == '':
                application_directory = os.path.realpath(script) # executed in interactive interpreter
            else:
                binary_directory = os.path.dirname(os.path.realpath(script))
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
        return os.path.join(cls.directory, resource or u'')


class IconManager(object):
    __metaclass__ = Singleton

    max_size = 256

    def __init__(self):
        self.iconmap = {}

    def get(self, id):
        try:
            return self.iconmap[id]
        except KeyError:
            pixmap = QPixmap()
            filename = ApplicationData.get(os.path.join('images', id + '.png'))
            if pixmap.load(filename):
                icon = QIcon(pixmap)
                icon.filename = filename
            else:
                icon = None
            return self.iconmap.setdefault(id, icon)

    def store_data(self, id, data):
        directory = ApplicationData.get('images')
        filename = os.path.join(directory, id + '.png')
        makedirs(directory)
        pixmap = QPixmap()
        if data is not None and pixmap.loadFromData(data):
            if pixmap.size().width() > self.max_size or pixmap.size().height() > self.max_size:
                pixmap = pixmap.scaled(self.max_size, self.max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pixmap.save(filename)
            icon = QIcon(pixmap)
            icon.filename = filename
        else:
            unlink(filename)
            icon = None
        self.iconmap[id] = icon
        return icon

    def store_file(self, id, file):
        directory = ApplicationData.get('images')
        filename = os.path.join(directory, id + '.png')
        if filename == os.path.normpath(file):
            return self.iconmap.get(id, None)
        makedirs(directory)
        pixmap = QPixmap()
        if file is not None and pixmap.load(file):
            if pixmap.size().width() > self.max_size or pixmap.size().height() > self.max_size:
                pixmap = pixmap.scaled(self.max_size, self.max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pixmap.save(filename)
            icon = QIcon(pixmap)
            icon.filename = filename
        else:
            unlink(filename)
            icon = None
        self.iconmap[id] = icon
        return icon

    def remove(self, id):
        self.iconmap.pop(id, None)
        unlink(ApplicationData.get(os.path.join('images', id + '.png')))


