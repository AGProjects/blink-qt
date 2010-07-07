# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['QSingleton', 'call_in_gui_thread', 'call_later', 'run_in_gui_thread', 'call_in_auxiliary_thread', 'run_in_auxiliary_thread']

from PyQt4.QtCore import QObject, QTimer
from PyQt4.QtGui import QApplication
from application.python.decorator import decorator, preserve_signature
from application.python.util import Singleton

from blink.event import CallFunctionEvent


class QSingleton(Singleton, type(QObject)):
    """A metaclass for making Qt objects singletons"""


def call_in_gui_thread(function, *args, **kw):
    application = QApplication.instance()
    application.postEvent(application, CallFunctionEvent(function, args, kw))


def call_later(interval, function, *args, **kw):
    interval = int(interval*1000)
    QTimer.singleShot(interval, lambda: function(*args, **kw))


@decorator
def run_in_gui_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        application = QApplication.instance()
        application.postEvent(application, CallFunctionEvent(func, args, kw))
    return wrapper


def call_in_auxiliary_thread(function, *args, **kw):
    application = QApplication.instance()
    application.postEvent(application.auxiliary_thread, CallFunctionEvent(function, args, kw))


def run_in_auxiliary_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        application = QApplication.instance()
        application.postEvent(application.auxiliary_thread, CallFunctionEvent(func, args, kw))
    return wrapper


