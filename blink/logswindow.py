import threading

from PyQt6 import uic
from PyQt6 import QtCore, QtWidgets

from application.python import Null
from application.notification import IObserver, NotificationCenter
from blink.logging import ActivityLog
from blink.resources import ApplicationData, Resources
from blink.util import run_in_gui_thread
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implementer
from datetime import datetime

ui_class, base_class = uic.loadUiType(Resources.get('logs_window.ui'))

@implementer(IObserver)
class LogsWindow(base_class, ui_class):

    # Activity lines arrive from any thread; they are queued and flushed to
    # the view in batches so a burst (e.g. a journal import) does not flood
    # the GUI thread with one event per line.
    activity_flush_interval = 150  # ms
    activity_buffer_limit = 4000

    activity_queued = QtCore.pyqtSignal()

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
        notification_center.add_observer(self, name='UILogMessage')
            
        self._siptrace_packet_count = 0
        self._siptrace_start_time = datetime.now()

        self._activity_lock = threading.Lock()
        self._activity_lines = []
        self._activity_dropped = 0
        # single shot, armed only when lines are waiting, so no Python code
        # runs periodically in the GUI thread when the log is idle
        self._activity_timer = QtCore.QTimer(self)
        self._activity_timer.setSingleShot(True)
        self._activity_timer.setInterval(self.activity_flush_interval)
        self._activity_timer.timeout.connect(self._flush_activity)
        self.activity_queued.connect(self._schedule_activity_flush)  # queued when emitted from other threads

    def updateCheckedButton(self):
        settings = SIPSimpleSettings()
        current_tab = self.logsTabWidget.currentWidget().objectName()
        if current_tab in ('activity', 'rtp'):
            # the activity and RTP logs are always on
            self.log_enabled_button.setVisible(False)
            return
        self.log_enabled_button.setVisible(True)
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
        ActivityLog().set_gui_logger(self._queue_activity)

    def closeEvent(self, event):
        QtCore.QSettings().setValue("logs_window/geometry", self.saveGeometry())
        # lines keep going to activity.txt and to the in-memory backlog,
        # which is replayed when the window is shown again
        ActivityLog().detach_gui_logger()
        self._activity_timer.stop()
        self._flush_activity()
        super(LogsWindow, self).closeEvent(event)

    def _queue_activity(self, level, timestamp, message):
        line = '%s %s' % (timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], message if level == 'INFO' else '%s: %s' % (level, message))
        with self._activity_lock:
            was_empty = not self._activity_lines and not self._activity_dropped
            if len(self._activity_lines) >= self.activity_buffer_limit:
                self._activity_dropped += 1
            else:
                self._activity_lines.append(line)
        if was_empty:
            self.activity_queued.emit()

    def _schedule_activity_flush(self):
        if not self._activity_timer.isActive():
            self._activity_timer.start()

    def _flush_activity(self):
        with self._activity_lock:
            lines, self._activity_lines = self._activity_lines, []
            dropped, self._activity_dropped = self._activity_dropped, 0
        if dropped:
            lines.append('... %d lines not shown here, see %s' % (dropped, ActivityLog().filename))
        if lines:
            self.activity_logs_view.appendPlainText('\n'.join(lines))

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
            
    def _NH_UILogMessage(self, notification):
        section = notification.data.section
        message = notification.data.message
        try:
            view = getattr(self, '%s_logs_view' % section)
        except AttributeError:
            pass
        else:
            view.appendPlainText(message)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.sender is settings:
            current_tab = self.logsTabWidget.currentWidget().objectName()
            for section in ('sip', 'msrp', 'xcap', 'messaging'):
                if 'logs.trace_%s' % section in notification.data.modified:
                    self.updateCheckedButton()
                    break
