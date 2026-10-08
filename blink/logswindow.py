import threading

from collections import deque

from PyQt6 import uic
from PyQt6 import QtCore, QtGui, QtWidgets

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

    # SIP tab: categories in the order of the sip_category combo box, and colors
    sip_categories = (None, 'sessions', 'subscriptions', 'register', 'messages')
    sip_entry_limit = 5000  # SIP messages kept for refiltering
    sip_received_color = '#2f7de1'
    sip_sending_color = '#e8890c'
    sip_error_color = '#e53935'

    def __init__(self, parent=None):
        super(LogsWindow, self).__init__(parent)

        with Resources.directory:
            self.setupUi()

        # after setupUi, which sets the default size from the .ui file
        geometry = QtCore.QSettings().value("logs_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

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

        # every activity line shown so far, so the filter can be changed or cleared
        self._activity_all = deque(maxlen=self.activity_logs_view.maximumBlockCount() or 50000)
        self._activity_filter_text = ''
        self._filter_timer = QtCore.QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)
        self._filter_timer.timeout.connect(self._apply_activity_filter)
        self.activity_filter.textChanged.connect(lambda text: self._filter_timer.start())

        # SIP messages, filtered by category and text like the activity lines
        self._sip_entries = deque(maxlen=self.sip_entry_limit)
        self._sip_filter_text = ''
        self._sip_category = None
        self._sip_filter_timer = QtCore.QTimer(self)
        self._sip_filter_timer.setSingleShot(True)
        self._sip_filter_timer.setInterval(200)
        self._sip_filter_timer.timeout.connect(self._apply_sip_filter)
        self.sip_filter.textChanged.connect(lambda text: self._sip_filter_timer.start())
        self.sip_logs_view.setMaximumBlockCount(200000)
        self._sip_normal_format = QtGui.QTextCharFormat()
        self._sip_bold_format = QtGui.QTextCharFormat()
        self._sip_bold_format.setFontWeight(QtGui.QFont.Weight.Bold)
        self._sip_error_format = QtGui.QTextCharFormat(self._sip_bold_format)
        self._sip_error_format.setForeground(QtGui.QColor(self.sip_error_color))
        self._sip_received_format = QtGui.QTextCharFormat()
        self._sip_received_format.setForeground(QtGui.QColor(self.sip_received_color))
        self._sip_sending_format = QtGui.QTextCharFormat()
        self._sip_sending_format.setForeground(QtGui.QColor(self.sip_sending_color))
        try:
            index = int(QtCore.QSettings().value('logs_window/sip_category', 0))
        except (TypeError, ValueError):
            index = 0
        if 0 <= index < len(self.sip_categories):
            self.sip_category.setCurrentIndex(index)
            self._sip_category = self.sip_categories[index]
        self.sip_category.currentIndexChanged.connect(self._SH_SipCategoryChanged)

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
            self._activity_all.extend(lines)
            if self._activity_filter_text:
                lines = [line for line in lines if self._activity_matches(line)]
            if lines:
                self.activity_logs_view.appendPlainText('\n'.join(lines))

    def _activity_matches(self, line):
        return self._activity_filter_text in line.lower()

    def _apply_activity_filter(self):
        self._activity_filter_text = self.activity_filter.text().strip().lower()
        if self._activity_filter_text:
            lines = [line for line in self._activity_all if self._activity_matches(line)]
        else:
            lines = self._activity_all
        view = self.activity_logs_view
        view.setPlainText('\n'.join(lines))
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())

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
            
    def _SH_SipCategoryChanged(self, index):
        self._sip_category = self.sip_categories[index] if 0 <= index < len(self.sip_categories) else None
        QtCore.QSettings().setValue('logs_window/sip_category', index)
        self._apply_sip_filter()

    def _sip_matches(self, entry):
        if self._sip_category is not None:
            # with a category chosen, only the SIP messages of that category (no DNS lines)
            categories = getattr(entry, 'categories', None)
            if not categories or self._sip_category not in categories:
                return False
        return not self._sip_filter_text or self._sip_filter_text in entry.message.lower()

    def _apply_sip_filter(self):
        self._sip_filter_text = self.sip_filter.text().strip().lower()
        view = self.sip_logs_view
        view.setUpdatesEnabled(False)
        try:
            view.clear()
            for entry in self._sip_entries:
                if self._sip_matches(entry):
                    self._append_sip_entry(entry, scroll=False)
        finally:
            view.setUpdatesEnabled(True)
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())

    def _append_sip_entry(self, entry, scroll=True):
        view = self.sip_logs_view
        scrollbar = view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        cursor = QtGui.QTextCursor(view.document())
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        if not view.document().isEmpty():
            cursor.insertBlock()
        direction = getattr(entry, 'direction', None)
        if direction is None:
            # DNS lookups and other lines without SIP message details
            cursor.insertText(entry.message.rstrip('\n'), self._sip_normal_format)
        else:
            cursor.insertText('%s: ' % entry.timestamp, self._sip_normal_format)
            cursor.insertText('%s:' % direction, self._sip_received_format if direction == 'RECEIVED' else self._sip_sending_format)
            cursor.insertText(' %s\n%s\n' % (entry.header, entry.route), self._sip_normal_format)
            cursor.insertText(entry.first_line, self._sip_error_format if entry.error else self._sip_bold_format)
            if entry.rest:
                cursor.insertText('\n' + entry.rest, self._sip_normal_format)
            cursor.insertText('\n--', self._sip_normal_format)
        if scroll and at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _NH_UILogMessage(self, notification):
        section = notification.data.section
        message = notification.data.message
        if section == 'sip':
            entry = notification.data
            self._sip_entries.append(entry)
            if self._sip_matches(entry):
                self._append_sip_entry(entry)
            return
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
