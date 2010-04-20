# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['CallFunctionEvent']

from PyQt4.QtCore import QEvent

from blink.util import classproperty


class EventMeta(type(QEvent)):
    def __init__(cls, name, bases, dct):
        super(EventMeta, cls).__init__(name, bases, dct)
        cls.id = QEvent.registerEventType() if name != 'EventBase' else None


class EventBase(QEvent):
    __metaclass__ = EventMeta

    def __new__(cls, *args, **kw):
        if cls is EventBase:
            raise TypeError("EventBase cannot be directly instantiated")
        return super(EventBase, cls).__new__(cls)

    def __init__(self):
        super(EventBase, self).__init__(self.id)

    @classproperty
    def name(cls):
        return cls.__name__


class CallFunctionEvent(EventBase):
    def __init__(self, function, args, kw):
        super(CallFunctionEvent, self).__init__()
        self.function = function
        self.args = args
        self.kw = kw


