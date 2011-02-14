# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Blink account settings extensions."""

__all__ = ['AccountExtension', 'BonjourAccountExtension']

from sipsimple.account import BonjourMSRPSettings, MessageSummarySettings, MSRPSettings, NATTraversalSettings, RTPSettings, SIPSettings, TLSSettings, XCAPSettings
from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.configuration.datatypes import AudioCodecList, Hostname, MSRPConnectionModel, MSRPTransport, SIPTransportList
from sipsimple.util import user_info

from blink.configuration.datatypes import ApplicationDataPath, CustomSoundFile, DefaultPath, HTTPURL


class BonjourMSRPSettingsExtension(BonjourMSRPSettings):
    transport = Setting(type=MSRPTransport, default='tcp')


class BonjourSIPSettings(SettingsGroup):
    transport_order = Setting(type=SIPTransportList, default=SIPTransportList(['tcp', 'udp', 'tls']))


class MessageSummarySettingsExtension(MessageSummarySettings):
    enabled = Setting(type=bool, default=True)


class MSRPSettingsExtension(MSRPSettings):
    connection_model = Setting(type=MSRPConnectionModel, default='relay')


class NATTraversalSettingsExtension(NATTraversalSettings):
    use_msrp_relay_for_inbound = Setting(type=bool, default=True)


class PSTNSettings(SettingsGroup):
    idd_prefix = Setting(type=unicode, default=None, nillable=True)
    prefix = Setting(type=unicode, default=None, nillable=True)


class RTPSettingsExtension(RTPSettings):
    audio_codec_order = Setting(type=AudioCodecList, default=None, nillable=True)
    inband_dtmf = Setting(type=bool, default=True)
    use_srtp_without_tls = Setting(type=bool, default=True)


class SIPSettingsExtension(SIPSettings):
    always_use_my_proxy = Setting(type=bool, default=True)
    register = Setting(type=bool, default=True)


class ServerSettings(SettingsGroup):
    conference_server = Setting(type=Hostname, default=None, nillable=True)
    settings_url = Setting(type=HTTPURL, default=None, nillable=True)


class SoundSettings(SettingsGroup):
    inbound_ringtone = Setting(type=CustomSoundFile, default=CustomSoundFile(DefaultPath), nillable=True)


class TLSSettingsExtension(TLSSettings):
    certificate = Setting(type=ApplicationDataPath, default=None, nillable=True)


class XCAPSettingsExtension(XCAPSettings):
    enabled = Setting(type=bool, default=True)


class AccountExtension(SettingsObjectExtension):
    display_name = Setting(type=unicode, default=user_info.fullname, nillable=True)
    message_summary = MessageSummarySettingsExtension
    msrp = MSRPSettingsExtension
    nat_traversal = NATTraversalSettingsExtension
    pstn = PSTNSettings
    rtp = RTPSettingsExtension
    server = ServerSettings
    sip = SIPSettingsExtension
    sounds = SoundSettings
    tls = TLSSettingsExtension
    xcap = XCAPSettingsExtension


class BonjourAccountExtension(SettingsObjectExtension):
    msrp = BonjourMSRPSettingsExtension
    rtp = RTPSettingsExtension
    sip = BonjourSIPSettings
    sounds = SoundSettings


