# Copyright (C) 2013 AG Projects. See LICENSE for details.
#

"""Blink addressbook settings extensions."""

__all__ = ['ContactExtension', 'GroupExtension']

from sipsimple.addressbook import ContactExtension, GroupExtension, SharedSetting
from sipsimple.configuration import Setting

from blink.configuration.datatypes import IconDescriptor


SharedSetting.set_namespace('ag-projects:blink')


class ContactExtension(ContactExtension):
    #auto_answer = SharedSetting(type=bool, default=False)
    default_uri = SharedSetting(type=str, nillable=True, default=None)
    preferred_media = SharedSetting(type=str, default='audio')
    icon = Setting(type=IconDescriptor, nillable=True, default=None)


class GroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    collapsed = Setting(type=bool, default=False)


