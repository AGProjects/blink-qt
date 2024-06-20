
"""Blink account settings extensions."""

__all__ = ['AccountExtension', 'BonjourAccountExtension']

from sipsimple.account import BonjourMSRPSettings, MessageSummarySettings, MSRPSettings, PresenceSettings, RTPSettings, SIPSettings, XCAPSettings
from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension, RuntimeSetting
from sipsimple.configuration.datatypes import AudioCodecList, Hostname, MSRPConnectionModel, MSRPTransport, NonNegativeInteger, SIPTransportList, VideoCodecList
from sipsimple.util import user_info

from blink.configuration.datatypes import ApplicationDataPath, HTTPURL, IconDescriptor, SoundFile
from blink.resources import Resources


class BonjourMSRPSettingsExtension(BonjourMSRPSettings):
    transport = Setting(type=MSRPTransport, default='tls')


class BonjourSIPSettings(SettingsGroup):
    transport_order = Setting(type=SIPTransportList, default=SIPTransportList(['tcp', 'udp', 'tls']))
    tls_name = Setting(type=str, default='Blink')
    auto_answer = Setting(type=bool, default=False)


class MessageSummarySettingsExtension(MessageSummarySettings):
    enabled = Setting(type=bool, default=True)


class MSRPSettingsExtension(MSRPSettings):
    connection_model = Setting(type=MSRPConnectionModel, default='relay')
    transport = Setting(type=MSRPTransport, default='tls')


class PresenceSettingsExtension(PresenceSettings):
    enabled = Setting(type=bool, default=True)


class PSTNSettings(SettingsGroup):
    idd_prefix = Setting(type=str, default=None, nillable=True)
    prefix = Setting(type=str, default=None, nillable=True)


class RTPSettingsExtension(RTPSettings):
    audio_codec_order = Setting(type=AudioCodecList, default=None, nillable=True)
    video_codec_order = Setting(type=VideoCodecList, default=None, nillable=True)
    inband_dtmf = Setting(type=bool, default=True)


class SIPSettingsExtension(SIPSettings):
    always_use_my_proxy = Setting(type=bool, default=True)
    register = Setting(type=bool, default=True)
    register_interval = Setting(type=NonNegativeInteger, default=600)
    subscribe_interval = Setting(type=NonNegativeInteger, default=600)
    publish_interval = Setting(type=NonNegativeInteger, default=600)
    tls_name = Setting(type=str, default=None, nillable=True)
    auto_answer = Setting(type=bool, default=True)


class ServerSettings(SettingsGroup):
    conference_server = Setting(type=Hostname, default=None, nillable=True)
    settings_url = Setting(type=HTTPURL, default=None, nillable=True)


class SMSSettings(SettingsGroup):
    use_cpim = Setting(type=bool, default=True)
    enable_iscomposing = Setting(type=bool, default=True)
    enable_imdn = Setting(type=bool, default=True)
    enable_pgp = Setting(type=bool, default=True)
    public_key = Setting(type=ApplicationDataPath, default=None, nillable=True)
    private_key = Setting(type=ApplicationDataPath, default=None, nillable=True)


class SMSSettingsExtension(SMSSettings):
    enable_history_synchronization = Setting(type=bool, default=True)
    enable_message_replication = Setting(type=bool, default=True)
    history_synchronization_url = Setting(type=HTTPURL, default=None, nillable=True)
    history_synchronization_token = Setting(type=str, default=None, nillable=True)
    history_synchronization_id = Setting(type=str, default=None, nillable=True)
    history_synchronization_timestamp = RuntimeSetting(type=str, default=None, nillable=True)


class SoundSettings(SettingsGroup):
    inbound_ringtone = Setting(type=SoundFile, default=None, nillable=True)


class XCAPSettingsExtension(XCAPSettings):
    enabled = Setting(type=bool, default=True)
    icon = RuntimeSetting(type=IconDescriptor, nillable=True, default=None)


class AccountExtension(SettingsObjectExtension):
    display_name = Setting(type=str, default=user_info.fullname, nillable=True)
    message_summary = MessageSummarySettingsExtension
    msrp = MSRPSettingsExtension
    pstn = PSTNSettings
    presence = PresenceSettingsExtension
    rtp = RTPSettingsExtension
    server = ServerSettings
    sip = SIPSettingsExtension
    sms = SMSSettingsExtension
    sounds = SoundSettings
    xcap = XCAPSettingsExtension


class BonjourAccountExtension(SettingsObjectExtension):
    msrp = BonjourMSRPSettingsExtension
    presence = PresenceSettingsExtension
    rtp = RTPSettingsExtension
    sip = BonjourSIPSettings
    sms = SMSSettings
    sounds = SoundSettings


