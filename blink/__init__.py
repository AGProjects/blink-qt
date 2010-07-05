# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['Blink']


__version__ = '0.1.0'
__date__    = 'July 5, 2010'


import sys

from PyQt4.QtGui import QApplication
from application import log
from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.account import Account, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.backend.file import FileBackend
from sipsimple.configuration.settings import SIPSimpleSettings

from blink.configuration.account import AccountExtension, BonjourAccountExtension
from blink.configuration.settings import SIPSimpleSettingsExtension
from blink.log import LogManager
from blink.mainwindow import MainWindow
from blink.resources import ApplicationData
from blink.sessions import SessionManager
from blink.update import UpdateManager
from blink.util import QSingleton, run_in_gui_thread


class Blink(QApplication):
    __metaclass__ = QSingleton

    implements(IObserver)

    def __init__(self):
        super(Blink, self).__init__(sys.argv)
        self.application = SIPApplication()
        self.main_window = MainWindow()

        self.update_manager = UpdateManager()
        self.main_window.check_for_updates_action.triggered.connect(self.update_manager.check_for_updates)
        self.main_window.check_for_updates_action.setVisible(self.update_manager != Null)

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)
        session_manager = SessionManager()
        session_manager.initialize(self.main_window, self.main_window.session_model)

    def run(self):
        from blink.util import call_in_gui_thread as call_later
        call_later(self._initialize_sipsimple)
        self.exec_()
        self.update_manager.shutdown()
        self.application.stop()

    def customEvent(self, event):
        handler = getattr(self, '_EH_%s' % event.name, Null)
        handler(event)

    def _EH_CallFunctionEvent(self, event):
        try:
            event.function(*event.args, **event.kw)
        except:
            log.error('Exception occured while calling function %s in the GUI thread' % event.function.__name__)
            log.err()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        log_manager = LogManager()
        log_manager.start()

    @run_in_gui_thread
    def _NH_SIPApplicationDidStart(self, notification):
        self.main_window.show()
        self.update_manager.initialize()

    def _NH_SIPApplicationDidEnd(self, notification):
        log_manager = LogManager()
        log_manager.stop()

    def _initialize_sipsimple(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.application)
        self.application.start(FileBackend(ApplicationData.get('config')))


