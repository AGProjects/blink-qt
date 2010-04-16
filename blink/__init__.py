# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['Blink']

import sys

from PyQt4.QtGui import QApplication

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


