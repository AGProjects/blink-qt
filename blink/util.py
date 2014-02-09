# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['QSingleton', 'call_in_gui_thread', 'call_later', 'run_in_gui_thread']

from PyQt4.QtCore import QObject, QThread, QTimer
from PyQt4.QtGui import QApplication
from application.python.decorator import decorator, preserve_signature
from application.python.types import Singleton

from blink.event import CallFunctionEvent


class QSingleton(Singleton, type(QObject)):
    """A metaclass for making Qt objects singletons"""


def call_in_gui_thread(function, *args, **kw):
    application = QApplication.instance()
    if application.thread() is QThread.currentThread():
        function(*args, **kw)
    else:
        application.postEvent(application, CallFunctionEvent(function, args, kw))


def call_later(interval, function, *args, **kw):
    QTimer.singleShot(int(interval*1000), lambda: function(*args, **kw))


@decorator
def run_in_gui_thread(function):
    @preserve_signature(function)
    def wrapper(*args, **kw):
        application = QApplication.instance()
        if application.thread() is QThread.currentThread():
            function(*args, **kw)
        else:
            application.postEvent(application, CallFunctionEvent(function, args, kw))
    return wrapper


