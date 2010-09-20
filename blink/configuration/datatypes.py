# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Definitions of datatypes for use in settings extensions."""

__all__ = ['ApplicationDataPath', 'SoundFile', 'DefaultPath', 'CustomSoundFile', 'HTTPURL', 'AuthorizationToken', 'InvalidToken']

import os
import re
from urlparse import urlparse

from sipsimple.configuration.datatypes import Hostname

from blink.resources import ApplicationData


class ApplicationDataPath(unicode):
    def __new__(cls, path):
        path = os.path.normpath(path)
        if path.startswith(ApplicationData.directory+os.path.sep):
            path = path[len(ApplicationData.directory+os.path.sep):]
        return unicode.__new__(cls, path)

    @property
    def normalized(self):
        return ApplicationData.get(self)


class SoundFile(object):
    def __init__(self, path, volume=100):
        self.path = path
        self.volume = int(volume)
        if not (0 <= self.volume <= 100):
            raise ValueError('illegal volume level: %d' % self.volume)

    def __getstate__(self):
        return u'%s,%s' % (self.__dict__['path'], self.volume)

    def __setstate__(self, state):
        try:
            path, volume = state.rsplit(u',', 1)
        except ValueError:
            self.__init__(state)
        else:
            self.__init__(path, volume)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.path, self.volume)

    def _get_path(self):
        return ApplicationData.get(self.__dict__['path'])
    def _set_path(self, path):
        path = os.path.normpath(path)
        if path.startswith(ApplicationData.directory+os.path.sep):
            path = path[len(ApplicationData.directory+os.path.sep):]
        self.__dict__['path'] = path
    path = property(_get_path, _set_path)
    del _get_path, _set_path


class DefaultPath(object):
    def __repr__(self):
        return self.__class__.__name__

class CustomSoundFile(object):
    def __init__(self, path=DefaultPath, volume=100):
        self.path = path
        self.volume = int(volume)
        if self.volume < 0 or self.volume > 100:
            raise ValueError('illegal volume level: %d' % self.volume)

    def __getstate__(self):
        if self.path is DefaultPath:
            return u'default'
        else:
            return u'file:%s,%s' % (self.__dict__['path'], self.volume)

    def __setstate__(self, state):
        match = re.match(r'^(?P<type>default|file:)(?P<path>.+?)?(,(?P<volume>\d+))?$', state)
        if match is None:
            raise ValueError('illegal value: %r' % state)
        data = match.groupdict()
        if data.pop('type') == 'default':
            data['path'] = DefaultPath
        data['volume'] = data['volume'] or 100
        self.__init__(**data)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.path, self.volume)

    def _get_path(self):
        path = self.__dict__['path']
        return path if path is DefaultPath else ApplicationData.get(path)
    def _set_path(self, path):
        if path is not DefaultPath:
            path = os.path.normpath(path)
            if path.startswith(ApplicationData.directory+os.path.sep):
                path = path[len(ApplicationData.directory+os.path.sep):]
        self.__dict__['path'] = path
    path = property(_get_path, _set_path)
    del _get_path, _set_path


class HTTPURL(unicode):
    def __new__(cls, value):
        value = unicode(value)
        url = urlparse(value)
        if url.scheme not in (u'http', u'https'):
            raise ValueError("illegal HTTP URL scheme (http and https only): %s" % url.scheme)
        Hostname(url.hostname)
        if url.port is not None and not (0 < url.port < 65536):
            raise ValueError("illegal port value: %d" % url.port)
        return value


class AuthorizationTokenMeta(type):
    def __init__(cls, name, bases, dic):
        super(AuthorizationTokenMeta, cls).__init__(name, bases, dic)
        cls._instances = {}
    def __call__(cls, *args):
        if len(args) > 1:
            raise TypeError('%s() takes at most 1 argument (%d given)' % (cls.__name__, len(args)))
        key = args[0] if args else ''
        if key not in cls._instances:
            cls._instances[key] = super(AuthorizationTokenMeta, cls).__call__(*args)
        return cls._instances[key]

class AuthorizationToken(str):
    __metaclass__ = AuthorizationTokenMeta
    def __repr__(self):
        if self is InvalidToken:
            return 'InvalidToken'
        else:
            return '%s(%s)' % (self.__class__.__name__, str.__repr__(self))

InvalidToken = AuthorizationToken() # a valid token is never empty


