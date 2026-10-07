"""The unread count on Blink's icon in the dock (Ubuntu Dock, Dash to Dock, KDE, Plank).

The Unity launcher API: a com.canonical.Unity.LauncherEntry.Update signal on
the session bus, naming the application by its desktop file
(application://blink.desktop), with count and count-visible. Sent with Gio
(python3-gi), so the types are the ones the docks expect (count is an int64).
Without a session bus, or on other platforms, nothing happens.
"""

import sys

from blink.logging import ActivityLog


__all__ = ['LauncherBadge']


class LauncherBadge(object):
    interface = 'com.canonical.Unity.LauncherEntry'
    path = '/com/agprojects/blink/launcher'
    desktop_file = 'blink.desktop'

    def __init__(self):
        self.count = None
        self._bus = None
        self._failed = not sys.platform.startswith('linux')

    def _connection(self):
        if self._bus is None and not self._failed:
            try:
                from gi.repository import Gio
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except Exception as e:
                self._failed = True
                ActivityLog().info(f'[ui] No unread count on the dock icon: {e}')
        return self._bus

    def set_count(self, count):
        count = max(int(count or 0), 0)
        if count == self.count:
            return
        bus = self._connection()
        if bus is None:
            return
        try:
            from gi.repository import GLib
            properties = {'count': GLib.Variant('x', count), 'count-visible': GLib.Variant('b', count > 0)}
            bus.emit_signal(None, self.path, self.interface, 'Update', GLib.Variant('(sa{sv})', (f'application://{self.desktop_file}', properties)))
        except Exception as e:
            ActivityLog().warning(f'[ui] Cannot show the unread count on the dock icon: {e}')
            return
        self.count = count
