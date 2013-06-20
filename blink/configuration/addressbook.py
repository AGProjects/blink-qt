# Copyright (C) 2013 AG Projects. See LICENSE for details.
#

"""Blink addressbook settings extensions."""

__all__ = ['ContactExtension', 'GroupExtension']

from sipsimple.addressbook import ContactExtension, GroupExtension, PresenceSettings, SharedSetting
from sipsimple.configuration import Setting, RuntimeSetting

from blink.configuration.datatypes import IconDescriptor


SharedSetting.set_namespace('ag-projects:blink')


class PresenceSettingsExtension(PresenceSettings):
    state = RuntimeSetting(type=unicode, nillable=True, default=None)
    note = RuntimeSetting(type=unicode, nillable=True, default=None)


class ContactExtension(ContactExtension):
    presence = PresenceSettingsExtension
    icon = Setting(type=IconDescriptor, nillable=True, default=None)
    alternate_icon = Setting(type=IconDescriptor, nillable=True, default=None)
    preferred_media = SharedSetting(type=str, default='audio')
    #auto_answer = SharedSetting(type=bool, default=False)


class GroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    collapsed = Setting(type=bool, default=False)


