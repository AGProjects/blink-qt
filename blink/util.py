
from PyQt5.QtCore import QObject, QThread, QTimer
from PyQt5.QtWidgets import QApplication
from application.python.decorator import decorator, preserve_signature
from application.python.descriptor import classproperty
from application.python.types import Singleton
from functools import partial
from threading import Event
from sys import exc_info

from blink.event import CallFunctionEvent


__all__ = ['QSingleton', 'call_in_gui_thread', 'call_later', 'run_in_gui_thread']


class QSingleton(Singleton, type(QObject)):
    """A metaclass for making Qt objects singletons"""


def call_later(interval, function, *args, **kw):
    QTimer.singleShot(int(interval*1000), lambda: function(*args, **kw))


def call_in_gui_thread(function, *args, **kw):
    application = Application.instance
    if QThread.currentThread() is Application.gui_thread:
        return function(*args, **kw)
    else:
        application.postEvent(application, CallFunctionEvent(function, args, kw))


@decorator
def run_in_gui_thread(function=None, wait=False):
    if function is not None:
        @preserve_signature(function)
        def function_wrapper(*args, **kw):
            application = Application.instance
            if QThread.currentThread() is Application.gui_thread:
                return function(*args, **kw)
            else:
                if wait:
                    executor = FunctionExecutor(function)
                    application.postEvent(application, CallFunctionEvent(executor, args, kw))
                    return executor.wait()
                else:
                    application.postEvent(application, CallFunctionEvent(function, args, kw))
        return function_wrapper
    else:
        return partial(run_in_gui_thread, wait=wait)


class Application(object):
    __attributes__ = {}

    @classproperty
    def instance(cls):
        try:
            return cls.__attributes__['instance']
        except KeyError:
            return cls.__attributes__.setdefault('instance', QApplication.instance())

    @classproperty
    def gui_thread(cls):
        try:
            return cls.__attributes__['gui_thread']
        except KeyError:
            return cls.__attributes__.setdefault('gui_thread', cls.instance.thread())


class FunctionExecutor(object):
    __slots__ = 'function', 'event', 'result', 'exception', 'traceback'

    def __init__(self, function):
        self.function = function
        self.event = Event()
        self.result = None
        self.exception = None
        self.traceback = None

    def __call__(self, *args, **kw):
        try:
            self.result = self.function(*args, **kw)
        except BaseException as exception:
            self.exception = exception
            self.traceback = exc_info()[2]
        finally:
            self.event.set()

    def wait(self):
        self.event.wait()
        if self.exception is not None:
            raise type(self.exception), self.exception, self.traceback
        else:
            return self.result


