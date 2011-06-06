# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['IUpdateManager', 'UpdateManager']

import sys

from application.python import Null
from zope.interface import Interface


class IUpdateManager(Interface):
    def initialize(self):
        pass
    def shutdown(self):
        pass
    def check_for_updates(self):
        pass


if sys.platform == 'win32':
    try:
        from blink.update.windows import UpdateManager
    except (AttributeError, ImportError, RuntimeError):
        UpdateManager = Null
else:
    UpdateManager = Null

