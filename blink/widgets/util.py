# Copyright (c) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['QtDynamicProperty']


from PyQt4.QtCore import QVariant


class QtDynamicProperty(object):
    def __init__(self, name, type=unicode):
        self.name = name
        self.type = type
    def __get__(self, obj, objtype):
        value = self if obj is None else obj.property(self.name).toPyObject()
        return value if value in (self, None) else self.type(value)
    def __set__(self, obj, value):
        obj.setProperty(self.name, QVariant(value if value is None else self.type(value)))
    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


