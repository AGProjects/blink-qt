# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['Blink']

import sys

from PyQt4.QtGui import QApplication
from application.python.util import Singleton

from blink.mainwindow import MainWindow


class Blink(object):
    __metaclass__ = Singleton

    def __init__(self):
        self.app = QApplication(sys.argv)
        self.main_window = MainWindow()

    def run(self):
        self.main_window.show()
        self.app.exec_()


