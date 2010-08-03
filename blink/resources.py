# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Provide access to Blink's resources"""

__all__ = ['ApplicationData', 'Resources', 'IconCache']

import cPickle as pickle
import os
import platform
import sys

from PyQt4.QtCore import Qt
from PyQt4.QtGui  import QPixmap
from application import log
from application.python.util import Singleton
from application.system import unlink
from collections import deque
from hashlib import sha512
from sipsimple.util import classproperty, makedirs


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


class FileInfo(object):
    def __init__(self, name):
        self.name = name
        try:
            stat = os.stat(ApplicationData.get(os.path.join('images', name)))
        except OSError:
            self.mtime = None
            self.size = None
        else:
            self.mtime = stat.st_mtime
            self.size = stat.st_size

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, FileInfo) and (self.name, self.mtime, self.size) == (other.name, other.mtime, other.size)

    def __ne__(self, other):
        return not self.__eq__(other)


class FileMapping(object):
    def __init__(self, source, destination):
        self.source = source
        self.destination = destination


class IconCache(object):
    __metaclass__ = Singleton

    def __init__(self):
        makedirs(ApplicationData.get('images'))
        try:
            self.filemap = pickle.load(open(ApplicationData.get(os.path.join('images', '.cached_icons.map'))))
        except Exception:
            self.filemap = {}
        all_names = set('cached_icon_%04d.png' % x for x in xrange(1, 10000))
        used_names = set(os.listdir(ApplicationData.get('images')))
        self.available_names = deque(sorted(all_names - used_names))

    def store(self, filename, pixmap=None):
        if filename is None:
            return None
        if not os.path.isabs(filename):
            return filename
        if filename.startswith(ApplicationData.directory + os.path.sep):
            return filename[len(ApplicationData.directory + os.path.sep):]
        try:
            file_mapping = self.filemap[filename]
        except KeyError:
            pass
        else:
            source_info = FileInfo(filename)
            destination_info = FileInfo(file_mapping.destination.name)
            if (source_info, destination_info) == (file_mapping.source, file_mapping.destination):
                return destination_info.name
        try:
            destination_name = os.path.join('images', self.available_names.popleft())
        except IndexError:
            # No more available file names. Return original file for now
            return filename
        if pixmap is None:
            pixmap = QPixmap()
            if pixmap.load(filename):
                pixmap = pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        makedirs(ApplicationData.get('images'))
        if pixmap.save(ApplicationData.get(destination_name)):
            source_info = FileInfo(filename)
            destination_info = FileInfo(destination_name)
            file_mapping = FileMapping(source_info, destination_info)
            self.filemap[filename] = file_mapping
            map_filename = ApplicationData.get(os.path.join('images', '.cached_icons.map'))
            map_tempname = map_filename + '.tmp'
            try:
                file = open(map_tempname, 'wb')
                pickle.dump(self.filemap, file)
                file.close()
                if sys.platform == 'win32':
                    unlink(map_filename)
                os.rename(map_tempname, map_filename)
            except Exception, e:
                log.error("could not save icon cache file mappings: %s" % e)
            return destination_name
        else:
            self.available_names.appendleft(os.path.basename(destination_name))
            return filename

    def store_image(self, data):
        if data is None:
            return None
        data_hash = sha512(data).hexdigest()
        try:
            return self.filemap[data_hash].destination
        except KeyError:
            pass
        try:
            destination_name = os.path.join('images', self.available_names.popleft())
        except IndexError:
            # No more available file names.
            return None
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            pixmap = pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        makedirs(ApplicationData.get('images'))
        if pixmap.save(ApplicationData.get(destination_name)):
            file_mapping = FileMapping(data_hash, destination_name)
            self.filemap[data_hash] = file_mapping
            map_filename = ApplicationData.get(os.path.join('images', '.cached_icons.map'))
            map_tempname = map_filename + '.tmp'
            try:
                file = open(map_tempname, 'wb')
                pickle.dump(self.filemap, file)
                file.close()
                if sys.platform == 'win32':
                    unlink(map_filename)
                os.rename(map_tempname, map_filename)
            except Exception, e:
                log.error("could not save icon cache file mappings: %s" % e)
            return destination_name
        else:
            self.available_names.appendleft(os.path.basename(destination_name))
            return None

