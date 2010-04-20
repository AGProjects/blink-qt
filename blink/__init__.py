# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['Blink']

import sys

from PyQt4.QtGui import QApplication
from application import log
from application.python.util import Null

from blink.mainwindow import MainWindow
from blink.util import QSingleton


class Blink(QApplication):
    __metaclass__ = QSingleton

    def __init__(self):
        super(Blink, self).__init__(sys.argv)
        self.main_window = MainWindow()

    def run(self):
        self.main_window.show()
        self.exec_()

    def customEvent(self, event):
        handler = getattr(self, '_EH_%s' % event.name, Null)
        handler(event)

    def _EH_CallFunctionEvent(self, event):
        try:
            event.function(*event.args, **event.kw)
        except:
            log.error('Exception occured while calling function %s in the GUI thread' % event.function.__name__)
            log.err()


