"""Taking a screenshot for sending: through the XDG desktop portal, as a sandboxed or
Wayland application has to (the portal shows the desktop's own picker: area, window or
screen), else with a screenshot tool found on the system.

    PortalScreenshot.take(done) calls done(path) with the saved picture, or done(None)
    when the user cancelled or nothing could take it.
"""

import os
import shutil
import tempfile
import uuid

from PyQt6.QtCore import QObject, QProcess, QUrl, pyqtSlot
try:
    from PyQt6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage, QDBusObjectPath, QDBusVariant
except ImportError:
    QDBusConnection = QDBusInterface = QDBusObjectPath = QDBusVariant = None
    QDBusMessage = object

from blink.logging import ActivityLog


__all__ = ['PortalScreenshot']


class PortalScreenshot(QObject):
    service = 'org.freedesktop.portal.Desktop'
    path = '/org/freedesktop/portal/desktop'
    interface = 'org.freedesktop.portal.Screenshot'
    request_interface = 'org.freedesktop.portal.Request'

    # tried in order when there is no portal: (program, arguments with {path})
    tools = (('gnome-screenshot', ['-a', '-f', '{path}']),
             ('spectacle', ['-b', '-n', '-r', '-o', '{path}']),
             ('xfce4-screenshooter', ['-r', '-s', '{path}']),
             ('scrot', ['-s', '{path}']),
             ('import', ['{path}']))

    _busy = None        # one screenshot at a time

    @classmethod
    def take(cls, done):
        if cls._busy is not None:
            return
        cls._busy = cls(done)
        cls._busy._start()

    def __init__(self, done):
        super().__init__()
        self.done = done
        self.request_path = None
        self.process = None

    def _finish(self, path):
        PortalScreenshot._busy = None
        if path:
            ActivityLog().info(f'[ui] Screenshot taken: {path}')
        try:
            self.done(path)
        finally:
            self.deleteLater()

    def _start(self):
        if not self._start_portal():
            self._start_tool()

    # The portal

    def _start_portal(self):
        if QDBusConnection is None:
            return False
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        token = 'blink' + uuid.uuid4().hex[:12]
        sender = bus.baseService().lstrip(':').replace('.', '_')
        # subscribe before asking, at the path the portal will answer on (it can answer at once)
        self.request_path = f'{self.path}/request/{sender}/{token}'
        bus.connect(self.service, self.request_path, self.request_interface, 'Response', self._response)
        portal = QDBusInterface(self.service, self.path, self.interface, bus)
        if not portal.isValid():
            bus.disconnect(self.service, self.request_path, self.request_interface, 'Response', self._response)
            return False
        reply = portal.call('Screenshot', '', {'handle_token': token, 'interactive': True, 'modal': True})
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            ActivityLog().warning(f'[ui] The screenshot portal refused: {reply.errorMessage()}')
            bus.disconnect(self.service, self.request_path, self.request_interface, 'Response', self._response)
            return False
        arguments = reply.arguments()
        if arguments and isinstance(arguments[0], QDBusObjectPath) and arguments[0].path() != self.request_path:
            # an older portal answers at a path of its own
            bus.disconnect(self.service, self.request_path, self.request_interface, 'Response', self._response)
            self.request_path = arguments[0].path()
            bus.connect(self.service, self.request_path, self.request_interface, 'Response', self._response)
        return True

    @pyqtSlot(QDBusMessage)
    def _response(self, message):
        QDBusConnection.sessionBus().disconnect(self.service, self.request_path, self.request_interface, 'Response', self._response)
        arguments = message.arguments()
        code = arguments[0] if arguments else 2
        results = arguments[1] if len(arguments) > 1 else {}
        uri = results.get('uri') if isinstance(results, dict) else None
        while isinstance(uri, QDBusVariant):
            uri = uri.variant()
        if code != 0 or not uri:
            ActivityLog().info('[ui] Screenshot cancelled' if code == 1 else f'[ui] The screenshot portal gave no picture (response {code})')
            self._finish(None)
            return
        self._finish(QUrl(str(uri)).toLocalFile() or None)

    # A screenshot tool

    def _start_tool(self):
        for program, arguments in self.tools:
            executable = shutil.which(program)
            if executable:
                break
        else:
            ActivityLog().warning('[ui] Cannot take a screenshot: no desktop portal and no screenshot tool (gnome-screenshot, spectacle, scrot, ...)')
            self._finish(None)
            return
        self.tool_path = os.path.join(tempfile.gettempdir(), f'blink-screenshot-{uuid.uuid4().hex[:8]}.png')
        self.process = QProcess(self)
        self.process.finished.connect(self._tool_finished)
        self.process.start(executable, [argument.format(path=self.tool_path) for argument in arguments])

    def _tool_finished(self, code, status):
        path = self.tool_path if os.path.isfile(self.tool_path) and os.path.getsize(self.tool_path) else None
        self._finish(path)
