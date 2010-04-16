# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['QSingleton']

from PyQt4.QtCore import QObject
from application.python.util import Singleton


class QSingleton(Singleton, type(QObject)):
    """A metaclass for making Qt objects singletons"""


