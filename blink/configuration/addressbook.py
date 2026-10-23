"""Blink addressbook settings extensions."""

__all__ = ['ContactExtension', 'ContactURIExtension', 'GroupExtension']

from application.configuration.datatypes import Boolean

from sipsimple.addressbook import ContactExtension, ContactURIExtension, GroupExtension, PresenceSettings, SharedSetting
from sipsimple.configuration import Setting, RuntimeSetting

from blink.configuration.datatypes import IconDescriptor


# The bag Blink for macOS and Sylk Mobile write their contact attributes in
# (urn:ag-projects:sipsimple:xml:ns:addressbook), so all three clients read and
# write the same values. Qt used its own 'ag-projects:blink' bag before; nothing
# deletes that bag, but nothing reads it any more either.
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
    preferred_media = SharedSetting(type=str, default='messages')    # what double-click / Enter does: messages, audio or video
    auto_answer = SharedSetting(type=Boolean, default=False)
    organization = SharedSetting(type=str, default='')
    disable_smileys = SharedSetting(type=Boolean, default=False)
    # files this contact sends are fetched on their own only when allowed: nobody can push
    # files onto this device without having been granted it (blink.messagepane.fetch)
    auto_download_media = SharedSetting(type=Boolean, default=False)

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
    # The picture lives in the SylkServer account storage as avatars/<contact id>
    # (blink.account_storage); nothing about it is in XCAP. These remember work this
    # device still owes the server: a picture set here not uploaded yet, a removed one
    # whose DELETE has not succeeded yet.
    avatar_pending = Setting(type=bool, default=False)
    avatar_delete_pending = Setting(type=bool, default=False)


class ContactURIExtension(ContactURIExtension):
    position = SharedSetting(type=int, nillable=True)


class GroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    collapsed = Setting(type=bool, default=False)
    # what the group IS, shared with macOS and mobile: 'calls', 'tel', 'blocked',
    # 'conference', 'favorites'; empty for a user's own group (blink.group_kinds)
    kind = SharedSetting(type=str, default='')

    # who last changed the group and when (blink.addressbook_origin), never set by hand
    modified_by = SharedSetting(type=str, default='')
    modified_agent = SharedSetting(type=str, default='')
    modified_at = SharedSetting(type=str, default='')
    modified_reason = SharedSetting(type=str, default='')
    modified_hash = SharedSetting(type=str, default='')
