"""The desktop's Do Not Disturb (GNOME, Ubuntu) followed by Blink's Silent.

GNOME keeps Do Not Disturb as org.gnome.desktop.notifications show-banners (false
while it is on). Watched with gsettings monitor:
- Do Not Disturb turned on (or on when Blink starts): Silent is turned on.
- Do Not Disturb turned off: Silent is turned off again, if it was turned on here
  and the user has not turned it off meanwhile.
The user can still turn Silent off (or on) in Blink at any time; that does not
change the desktop's setting. Without gsettings or the schema, nothing is done.
"""

import shutil
import subprocess

from PyQt6.QtCore import QObject, QProcess

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implementer

from blink.logging import ActivityLog
from blink.util import QSingleton, run_in_gui_thread


__all__ = ['DesktopDoNotDisturb']


@implementer(IObserver)
class DesktopDoNotDisturb(QObject, metaclass=QSingleton):
    schema = 'org.gnome.desktop.notifications'
    key = 'show-banners'

    def __init__(self):
        super().__init__()
        self.process = None
        self.desktop_dnd = None     # the desktop's state as last seen; None: not known
        self.silenced = False       # Silent was turned on here, for the desktop's Do Not Disturb

    def start(self):
        if self.process is not None or shutil.which('gsettings') is None:
            return
        try:
            result = subprocess.run(['gsettings', 'get', self.schema, self.key], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return
        if result.returncode != 0:
            return      # not GNOME, or an old one without the key
        NotificationCenter().add_observer(self, name='CFGSettingsObjectDidChange')
        self._update(result.stdout.strip() == 'false', 'when Blink started')
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.start('gsettings', ['monitor', self.schema, self.key])

    def stop(self):
        if self.process is not None:
            self.process.kill()
            self.process.waitForFinished(1000)
            self.process = None
            NotificationCenter().remove_observer(self, name='CFGSettingsObjectDidChange')

    def _read(self):
        output = bytes(self.process.readAllStandardOutput()).decode(errors='replace')
        for line in output.splitlines():
            name, _, value = line.partition(':')
            if name.strip() == self.key and value.strip() in ('true', 'false'):
                self._update(value.strip() == 'false', 'on the desktop')

    def _update(self, dnd, when):
        if dnd == self.desktop_dnd:
            return
        self.desktop_dnd = dnd
        settings = SIPSimpleSettings()
        if dnd:
            if not settings.audio.silent:
                settings.audio.silent = True
                settings.save()
                self.silenced = True
                ActivityLog().info(f'[dnd] Do Not Disturb is on {when}: Silent turned on')
        elif self.silenced:
            self.silenced = False
            if settings.audio.silent:
                settings.audio.silent = False
                settings.save()
                ActivityLog().info(f'[dnd] Do Not Disturb is off {when}: Silent turned off')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'audio.silent' in notification.data.modified and not SIPSimpleSettings().audio.silent:
            self.silenced = False   # turned off by the user (or here): the desktop's off no longer has anything to undo
