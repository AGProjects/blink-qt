# Copyright (c) 2010-2013 AG Projects. See LICENSE for details.
#

__all__ = ['QtDynamicProperty', 'ContextMenuActions']

from PyQt4.QtCore import QPyNullVariant


class QtDynamicProperty(object):
    def __init__(self, name, type=unicode):
        self.name = name
        self.type = type
    def __get__(self, obj, objtype):
        if obj is None:
            return self
        value = obj.property(self.name)
        if isinstance(value, QPyNullVariant):
            value = self.type()
        return value
    def __set__(self, obj, value):
        if value is not None and not isinstance(value, self.type):
            value = self.type(value)
        obj.setProperty(self.name, value)
    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class ContextMenuActions(object):
    pass


