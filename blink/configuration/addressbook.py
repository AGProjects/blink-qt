"""Blink addressbook settings extensions."""

__all__ = ['ContactExtension', 'ContactURIExtension', 'GroupExtension', 'SharedSettingsMigration']

import os

from application.configuration.datatypes import Boolean
from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.addressbook import ContactExtension, ContactURIExtension, GroupExtension, PresenceSettings, SharedSetting
from sipsimple.configuration import Setting, RuntimeSetting

from blink.configuration.datatypes import IconDescriptor


# The bag Blink for macOS and Sylk Mobile write their contact attributes in
# (urn:ag-projects:sipsimple:xml:ns:addressbook), so all three clients read and
# write the same values. Qt used its own 'ag-projects:blink' bag until now;
# nothing deletes that bag, so going back restores every value. Values only Qt
# wrote there are carried over once by SharedSettingsMigration.
SharedSetting.set_namespace('ag-projects:sipsimple')


class PresenceSettingsExtension(PresenceSettings):
    state = RuntimeSetting(type=str, nillable=True, default=None)
    note = RuntimeSetting(type=str, nillable=True, default=None)


class ContactExtension(ContactExtension):
    presence = PresenceSettingsExtension
    icon = Setting(type=IconDescriptor, nillable=True, default=None)
    alternate_icon = Setting(type=IconDescriptor, nillable=True, default=None)

    # shared with Blink for macOS and Sylk Mobile. Booleans use Boolean: a value
    # applied from the XCAP document is the string 'true' or 'false'
    preferred_media = SharedSetting(type=str, default='audio')
    auto_answer = SharedSetting(type=Boolean, default=False)
    organization = SharedSetting(type=str, default='')
    disable_smileys = SharedSetting(type=Boolean, default=False)

    # who last changed the entry and when (blink.addressbook_origin), never set by hand
    modified_by = SharedSetting(type=str, default='')
    modified_agent = SharedSetting(type=str, default='')
    modified_at = SharedSetting(type=str, default='')
    modified_reason = SharedSetting(type=str, default='')
    modified_hash = SharedSetting(type=str, default='')

    # this device only, as on macOS: never written to the XCAP document
    chat_language = Setting(type=str, default=None, nillable=True)
    silence_notifications = Setting(type=bool, default=False)
    no_mediaproxy = Setting(type=bool, default=False)
    public_key = Setting(type=str, default=None, nillable=True)
    public_key_checksum = Setting(type=str, default=None, nillable=True)


class ContactURIExtension(ContactURIExtension):
    position = SharedSetting(type=int, nillable=True)


class GroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    collapsed = Setting(type=bool, default=False)


@implementer(IObserver)
class SharedSettingsMigration(object, metaclass=Singleton):
    """Carry the attributes Qt kept in the 'ag-projects:blink' bag to the shared one, once.

    After the namespace switch an XCAP reload no longer reads the old bag, but
    it only sets the names present in the document, so this machine keeps its
    own values. Once the addressbook has loaded, every contact whose
    preferred_media or auto_answer differs from the default is saved with
    those names marked changed, which writes them to the shared bag. A value
    another client already put there was applied by the reload, so it is
    written back unchanged. A marker file makes it happen once.
    """

    names = ('preferred_media', 'auto_answer')
    delay = 10  # seconds after the first reload, so the manager has applied it

    def __init__(self):
        self._started = False

    @property
    def marker(self):
        from blink.resources import ApplicationData
        return ApplicationData.get('addressbook-shared-settings.done')

    def start(self):
        if self._started or os.path.exists(self.marker):
            return
        self._started = True
        NotificationCenter().add_observer(self, name='XCAPManagerDidReloadData')

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_XCAPManagerDidReloadData(self, notification):
        NotificationCenter().remove_observer(self, name='XCAPManagerDidReloadData')
        from blink.util import call_in_gui_thread, call_later
        call_in_gui_thread(call_later, self.delay, self._migrate)

    def _migrate(self):
        from sipsimple.addressbook import AddressbookManager, Contact
        from blink.logging import ActivityLog
        manager = AddressbookManager()
        moved = 0
        try:
            with manager.transaction():
                for contact in manager.get_contacts():
                    names = [name for name in self.names if getattr(contact, name) != getattr(Contact, name).default]
                    if not names:
                        continue
                    for name in names:
                        getattr(Contact, name).dirty[contact] = True    # an unchanged value is not written otherwise
                    contact.save()
                    moved += 1
                    ActivityLog().info(f'[addressbook] Copied {", ".join(f"{name}={getattr(contact, name)}" for name in names)} of {contact.name or contact.id} to the shared attributes')
        except Exception as e:
            ActivityLog().error(f'[addressbook] Copying contact attributes to the shared attributes failed: {e!r}')
            return
        try:
            with open(self.marker, 'w') as marker:
                marker.write('ag-projects:sipsimple\n')
        except OSError as e:
            ActivityLog().warning(f'[addressbook] Cannot write {self.marker}: {e}')
        ActivityLog().info(f'[addressbook] Contact attributes now shared with Blink for macOS and Sylk Mobile: {moved} contacts carried over')
