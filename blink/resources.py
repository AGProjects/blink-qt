# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Provide access to Blink's resources"""

__all__ = ['Resources']

import os
import sys

# TODO: replace later with classproperty from sipsimple.util
def classproperty(function):
    class Descriptor(object):
        def __get__(self, instance, owner):
            return function(owner)
        def __set__(self, instance, value):
            raise AttributeError("read-only attribute cannot be set")
        def __delete__(self, instance):
            raise AttributeError("read-only attribute cannot be deleted")
    return Descriptor()


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
                cls._cached_directory = os.path.join(application_directory, 'resources')
            else:
                cls._cached_directory = os.path.join(application_directory, 'share', 'blink')
        return cls._cached_directory

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, resource)


