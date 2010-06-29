# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Blink account settings extensions."""

__all__ = ['AccountExtension', 'BonjourAccountExtension']

from sipsimple.account import PSTNSettings
from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.util import user_info

from blink.configuration.datatypes import CustomSoundFile, DefaultPath


class PSTNSettingsExtension(PSTNSettings):
    idd_prefix = Setting(type=unicode, default=None, nillable=True)


class SoundSettings(SettingsGroup):
    inbound_ringtone = Setting(type=CustomSoundFile, default=CustomSoundFile(DefaultPath), nillable=True)


class AccountExtension(SettingsObjectExtension):
    pstn = PSTNSettingsExtension
    sounds = SoundSettings

    display_name = Setting(type=str, default=user_info.fullname, nillable=True)


class BonjourAccountExtension(SettingsObjectExtension):
    sounds = SoundSettings


