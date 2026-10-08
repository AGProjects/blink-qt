"""Desktop notifications of incoming messages, as Blink for macOS shows them.

On Linux through the freedesktop notification service (org.freedesktop.Notifications,
the GNOME and KDE notification centre), with Gio from python3-gi: the sender's
name, what came (a message, a picture, a file -- never what it says or its name:
the notification centre and the lock screen are not the place to read it), Blink's icon.
One notification per conversation: a newer message replaces it ("3 new messages").
Clicking it opens the conversation in the message pane and brings Blink to the
front; reading the conversation withdraws it. Nothing is shown for a message in
the conversation the user is reading, for one's own messages from another
device, or for what the journal brings (only live messages notify).
"""

import html
import sys

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from blink.logging import ActivityLog
from blink.resources import Resources
from blink.util import run_in_gui_thread, translate


__all__ = ['MessageNotifier']




def _preview(content_type, content):
    return translate('notifications', 'New message')


def _file_preview(file):
    kind = str(getattr(file, 'type', '') or getattr(file, 'content_type', '') or '').lower()
    if kind.startswith('image/'):
        return translate('notifications', 'New picture')
    if kind.startswith('video/'):
        return translate('notifications', 'New video')
    if kind.startswith('audio/'):
        return translate('notifications', 'New audio message')
    return translate('notifications', 'New file')


@implementer(IObserver)
class MessageNotifier(object):
    bus_name = 'org.freedesktop.Notifications'
    path = '/org/freedesktop/Notifications'
    interface = 'org.freedesktop.Notifications'
    desktop_entry = 'blink'

    def __init__(self, main_window):
        self.main_window = main_window
        self.bus = None
        self.shown = {}             # conversation key: (notification id, count)
        self.targets = {}           # notification id: (conversation key, contact, contact_uri)
        if not sys.platform.startswith('linux'):
            return
        try:
            from gi.repository import Gio
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.bus.signal_subscribe(self.bus_name, self.interface, 'ActionInvoked', self.path, None, Gio.DBusSignalFlags.NONE, self._signal)
            self.bus.signal_subscribe(self.bus_name, self.interface, 'NotificationClosed', self.path, None, Gio.DBusSignalFlags.NONE, self._signal)
        except Exception as e:
            self.bus = None
            ActivityLog().info(f'[notifications] No desktop notifications: {e}')
            return
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkMessageIsParsed')
        notification_center.add_observer(self, name='BlinkSessionDidShareFile')
        notification_center.add_observer(self, name='BlinkMessagePaneDidReadConversation')
        notification_center.add_observer(self, name='BlinkConfirmReadMessagesOnOtherDevice')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    # What notifies

    def _NH_BlinkMessageIsParsed(self, notification):
        message = notification.data
        if getattr(message, 'direction', None) == 'outgoing':
            return
        content_type = str(getattr(message, 'content_type', '') or '').lower()
        if not (content_type.startswith('text/') and content_type not in ('text/pgp-public-key', 'text/pgp-private-key')):
            return
        self._notify(notification.sender, _preview(content_type, message.content))

    def _NH_BlinkSessionDidShareFile(self, notification):
        if getattr(notification.data, 'direction', None) != 'incoming':
            return
        self._notify(notification.sender, _file_preview(notification.data.file))

    def _NH_BlinkMessagePaneDidReadConversation(self, notification):
        self._withdraw(str(notification.data.remote_uri))

    def _NH_BlinkConfirmReadMessagesOnOtherDevice(self, notification):
        self._withdraw(str(getattr(notification.data, 'remote_uri', '') or ''))

    # Showing

    def _session_target(self, session):
        from blink.history import conversation_key
        from blink.uris import bare_instance_id
        contact = getattr(session, 'contact', None)
        contact_uri = getattr(session, 'contact_uri', None)
        if contact is None or contact_uri is None:
            return None
        if getattr(session, 'remote_instance_id', None):
            key = bare_instance_id(session.remote_instance_id)
        else:
            key = conversation_key(str(contact_uri.uri), getattr(session, 'account', None))
        return key, contact, contact_uri

    def _being_read(self, key):
        pane = getattr(self.main_window, 'message_pane', None)
        if pane is None or not pane.is_reading():
            return False
        return key in (getattr(pane, 'view_key', None) or (pane.key,))

    def _notify(self, session, text):
        if self.bus is None:
            return
        target = self._session_target(session)
        if target is None:
            return
        key, contact, contact_uri = target
        if self._being_read(key):
            return
        name = getattr(contact, 'name', '') or str(contact_uri.uri).split(':', 1)[-1]
        previous_id, count = self.shown.get(key, (0, 0))
        count += 1
        body = text if count == 1 else translate('notifications', '%d new messages') % count
        from gi.repository import GLib
        hints = {'desktop-entry': GLib.Variant('s', self.desktop_entry),
                 'category': GLib.Variant('s', 'im.received'),
                 'image-path': GLib.Variant('s', Resources.get('icons/blink.png'))}
        parameters = GLib.Variant('(susssasa{sv}i)', ('Blink', previous_id, Resources.get('icons/blink.png'), name,
                                                      html.escape(body, quote=False), ['default', translate('notifications', 'Open')], hints, -1))

        def sent(bus, result):
            try:
                notification_id = bus.call_finish(result).unpack()[0]
            except Exception as e:
                ActivityLog().warning(f'[notifications] Cannot show a desktop notification: {e}')
                return
            self.shown[key] = (notification_id, count)
            self.targets[notification_id] = (key, contact, contact_uri)
        self.bus.call(self.bus_name, self.path, self.interface, 'Notify', parameters, GLib.VariantType('(u)'), 0, -1, None, sent)
        ActivityLog().info(f'[notifications] New message from {key}' + (f' ({count} unread)' if count > 1 else ''))

    def _withdraw(self, key):
        shown = self.shown.pop(key, None)
        if shown is None or self.bus is None:
            return
        notification_id = shown[0]
        self.targets.pop(notification_id, None)
        from gi.repository import GLib
        self.bus.call(self.bus_name, self.path, self.interface, 'CloseNotification', GLib.Variant('(u)', (notification_id,)), None, 0, -1, None, None)

    # Answers from the notification service (GLib main context: the GUI thread)

    def _signal(self, bus, sender, path, interface, signal, parameters):
        values = parameters.unpack()
        notification_id = values[0]
        target = self.targets.get(notification_id)
        if target is None:
            return
        key, contact, contact_uri = target
        if signal == 'ActionInvoked':
            self.targets.pop(notification_id, None)
            self.shown.pop(key, None)
            ActivityLog().info(f'[notifications] Opened the conversation with {key} from its notification')
            self.main_window.show_conversation_in_pane(contact, contact_uri)
            self.main_window._bring_to_front()
        elif signal == 'NotificationClosed':
            self.targets.pop(notification_id, None)
            if self.shown.get(key, (None,))[0] == notification_id:
                self.shown.pop(key, None)
