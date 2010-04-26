# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Provide access to Blink's resources"""

__all__ = ['ApplicationData', 'Resources']

import os
import platform
import sys

from blink.util import classproperty


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
                cls._cached_directory = NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0]
            elif platform.system() == 'Windows':
                cls._cached_directory = os.path.join(os.environ['APPDATA'], 'Blink').decode(sys.getfilesystemencoding())
            else:
                cls._cached_directory = os.path.expanduser('~/.blink').decode(sys.getfilesystemencoding())
        return DirectoryContextManager(cls._cached_directory)

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, resource)


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
        return os.path.join(cls.directory, resource)


