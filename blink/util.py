# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['QSingleton', 'classproperty', 'call_in_gui_thread', 'run_in_gui_thread']

from PyQt4.QtCore import QObject
from application.python.decorator import decorator, preserve_signature
from application.python.util import Singleton


class QSingleton(Singleton, type(QObject)):
    """A metaclass for making Qt objects singletons"""


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


def call_in_gui_thread(function, *args, **kw):
    from blink import Blink
    from blink.event import CallFunctionEvent
    blink = Blink()
    blink.postEvent(blink, CallFunctionEvent(function, args, kw))


@decorator
def run_in_gui_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        from blink import Blink
        from blink.event import CallFunctionEvent
        blink = Blink()
        blink.postEvent(blink, CallFunctionEvent(func, args, kw))
    return wrapper


