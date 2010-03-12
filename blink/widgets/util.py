# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['QtDynamicProperty']


from PyQt4.QtCore import QVariant


class QtDynamicProperty(object):
    def __init__(self, name, type=str):
        self.name = name
        self.type = type
    def __get__(self, obj, objtype):
        return obj.property(self.name).toPyObject() if obj is not None else self
    def __set__(self, obj, value):
        obj.setProperty(self.name, QVariant(value))
    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


