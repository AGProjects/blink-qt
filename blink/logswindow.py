from PyQt5 import uic
from PyQt5 import QtCore, QtWidgets

from application.python import Null
from application.notification import IObserver, NotificationCenter
from blink.resources import ApplicationData, Resources
from blink.util import run_in_gui_thread
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implementer
from datetime import datetime

ui_class, base_class = uic.loadUiType(Resources.get('logs_window.ui'))

@implementer(IObserver)
class LogsWindow(base_class, ui_class):

    def __init__(self, parent=None):
        super(LogsWindow, self).__init__(parent)
        geometry = QtCore.QSettings().value("logs_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        with Resources.directory:
            self.setupUi()

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='UILogSip')
        notification_center.add_observer(self, name='UILogMsrp')
        notification_center.add_observer(self, name='UILogXcap')
        notification_center.add_observer(self, name='UILogMessaging')
            
        self._siptrace_packet_count = 0
        self._siptrace_start_time = datetime.now()

    def updateCheckedButton(self):
        settings = SIPSimpleSettings()
        current_tab = self.logsTabWidget.currentWidget().objectName()
        checked = getattr(settings.logs, 'trace_%s' % current_tab)
        self.log_enabled_button.setChecked(checked)

    def setupUi(self):
        super(LogsWindow, self).setupUi(self)
        self.setWindowTitle('Blink Logs')
        self.logsTabWidget.currentChanged.connect(self.tabChanged)
        self.log_enabled_button.clicked.connect(self._SH_EnabledButtonClicked)

    def tabChanged(self, index):
        self.updateCheckedButton()

    def show(self):
        super(LogsWindow, self).show()
        self.updateCheckedButton()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        QtCore.QSettings().setValue("logs_window/geometry", self.saveGeometry())
        super(LogsWindow, self).closeEvent(event)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @run_in_gui_thread
    def _NH_SIPApplicationDidStart(self, notification):
        self.logsTabWidget.setCurrentIndex(0)

    def _SH_EnabledButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        current_tab = self.logsTabWidget.currentWidget().objectName()
        setattr(settings.logs, 'trace_%s' % current_tab, checked)
        settings.save()
            
    def _NH_UILogSip(self, notification):
        self.sip_logs_view.appendPlainText(notification.data)

    def _NH_UILogMsrp(self, notification):
        self.msrp_logs_view.appendPlainText(notification.data)

    def _NH_UILogXcap(self, notification):
        self.xcap_logs_view.appendPlainText(notification.data)

    def _NH_UILogMessaging(self, notification):
        self.messages_logs_view.appendPlainText(notification.data)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.sender is settings:
            current_tab = self.logsTabWidget.currentWidget().objectName()
            for section in ('sip', 'msrp', 'xcap', 'messaging'):
                if 'logs.trace_%s' % section in notification.data.modified:
                    self.updateCheckedButton()
                    break
