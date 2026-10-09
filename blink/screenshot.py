"""Taking a screenshot for sending: through the XDG desktop portal, as a sandboxed or
Wayland application has to (the portal shows the desktop's own picker: area, window or
screen), else with a screenshot tool found on the system.

    PortalScreenshot.take(done) calls done(path) with the saved picture, or done(None)
    when the user cancelled or nothing could take it.

GNOME Shell's screenshot UI also puts the picture on the clipboard, and on some
versions the portal then answers with an error (response 2) and no file even though
the picture was taken. So the clipboard is watched while the screenshot is taken: a
picture that lands there is used when the portal gave none, and either way the
clipboard is put back as it was (or emptied), so the screenshot does not linger there.
"""

import os
import shutil
import tempfile
import uuid

from PyQt6.QtCore import QMimeData, QObject, QProcess, QTimer, QUrl, pyqtSlot
from PyQt6.QtGui import QGuiApplication
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

    clipboard_settle = 400  # ms to let the clipboard owner's change reach us after the portal answers

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
        self.saved_clipboard = None
        self.clipboard_changed = False

    def _finish(self, path, cancelled=False):
        QTimer.singleShot(self.clipboard_settle, lambda: self._complete(path, cancelled))

    def _complete(self, path, cancelled):
        PortalScreenshot._busy = None
        try:
            path = self._take_back_clipboard(path, cancelled)
        except Exception as e:
            ActivityLog().warning(f'[ui] Cannot restore the clipboard after the screenshot: {e!r}')
        if path:
            ActivityLog().info(f'[ui] Screenshot taken: {path}')
        try:
            self.done(path)
        finally:
            self.deleteLater()

    def _start(self):
        clipboard = QGuiApplication.clipboard()
        self.saved_clipboard = self._copy_mime(clipboard.mimeData())
        clipboard.dataChanged.connect(self._clipboard_changed)
        if not self._start_portal():
            self._start_tool()

    # The clipboard

    def _clipboard_changed(self):
        self.clipboard_changed = True

    @staticmethod
    def _copy_mime(source):
        if source is None:
            return None
        copy = QMimeData()
        if source.hasUrls():
            copy.setUrls(source.urls())
        if source.hasHtml():
            copy.setHtml(source.html())
        if source.hasText():
            copy.setText(source.text())
        if source.hasImage():
            copy.setImageData(source.imageData())
        return copy if copy.formats() else None

    def _take_back_clipboard(self, path, cancelled):
        """The picture the screenshot put on the clipboard, when there is no file, and the clipboard as it was."""
        clipboard = QGuiApplication.clipboard()
        clipboard.dataChanged.disconnect(self._clipboard_changed)
        if not self.clipboard_changed:
            return path
        image = clipboard.image()
        if image.isNull():
            return path
        if path is None and not cancelled:
            candidate = os.path.join(tempfile.gettempdir(), f'blink-screenshot-{uuid.uuid4().hex[:8]}.png')
            if image.save(candidate, 'PNG'):
                ActivityLog().info('[ui] The screenshot was taken from the clipboard')
                path = candidate
            else:
                ActivityLog().warning(f'[ui] Cannot write the screenshot from the clipboard to {candidate}')
        if self.saved_clipboard is not None:
            clipboard.setMimeData(self.saved_clipboard)
            self.saved_clipboard = None
        else:
            clipboard.clear()
        return path

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
            self._finish(None, cancelled=code == 1)
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
