# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Blink settings extensions."""

__all__ = ['BlinkSettings', 'SIPSimpleSettingsExtension']

import platform
import sys

from sipsimple.configuration import Setting, SettingsGroup, SettingsObject, SettingsObjectExtension
from sipsimple.configuration.datatypes import AudioCodecList, NonNegativeInteger, PositiveInteger, Path, SampleRate
from sipsimple.configuration.settings import AudioSettings, ChatSettings, FileTransferSettings, LogsSettings, RTPSettings, TLSSettings

from blink import __version__
from blink.configuration.datatypes import ApplicationDataPath, AuthorizationToken, HTTPURL, IconDescriptor, SoundFile, PresenceState, PresenceStateList
from blink.resources import Resources


class AnsweringMachineSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    answer_delay = Setting(type=NonNegativeInteger, default=10)
    max_recording = Setting(type=PositiveInteger, default=3)
    unavailable_message = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/unavailable_message.wav')), nillable=True)


class AudioSettingsExtension(AudioSettings):
    recordings_directory = Setting(type=ApplicationDataPath, default=ApplicationDataPath('recordings'))
    sample_rate = Setting(type=SampleRate, default=44100)


class ChatSettingsExtension(ChatSettings):
    auto_accept = Setting(type=bool, default=False)
    sms_replication = Setting(type=bool, default=True)
    history_directory = Setting(type=ApplicationDataPath, default=ApplicationDataPath('history'))


class FileTransferSettingsExtension(FileTransferSettings):
    auto_accept = Setting(type=bool, default=False)
    directory = Setting(type=Path, default=None, nillable=True)


class GoogleContactsSettings(SettingsGroup):
    authorization_token = Setting(type=AuthorizationToken, default=None, nillable=True)
    username = Setting(type=unicode, default=None, nillable=True)


class LogsSettingsExtension(LogsSettings):
    trace_sip = Setting(type=bool, default=False)
    trace_pjsip = Setting(type=bool, default=False)
    trace_msrp = Setting(type=bool, default=False)
    trace_xcap = Setting(type=bool, default=False)
    trace_notifications = Setting(type=bool, default=False)


class RTPSettingsExtension(RTPSettings):
    audio_codec_order = Setting(type=AudioCodecList, default=AudioCodecList(('G722', 'speex', 'GSM', 'iLBC', 'PCMU', 'PCMA')))


class ServerSettings(SettingsGroup):
    enrollment_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/enrollment.phtml")


class SoundSettings(SettingsGroup):
    inbound_ringtone = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/inbound_ringtone.wav')), nillable=True)
    outbound_ringtone = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/outbound_ringtone.wav')), nillable=True)
    message_received = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/message_received.wav')), nillable=True)
    message_sent = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/message_sent.wav')), nillable=True)
    file_received = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/file_received.wav')), nillable=True)
    file_sent = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/file_sent.wav')), nillable=True)
    play_message_alerts = Setting(type=bool, default=True)
    play_file_alerts = Setting(type=bool, default=True)


class TLSSettingsExtension(TLSSettings):
    ca_list = Setting(type=ApplicationDataPath, default=None, nillable=True)


class SIPSimpleSettingsExtension(SettingsObjectExtension):
    answering_machine = AnsweringMachineSettings
    audio = AudioSettingsExtension
    chat = ChatSettingsExtension
    file_transfer = FileTransferSettingsExtension
    google_contacts = GoogleContactsSettings
    logs = LogsSettingsExtension
    rtp = RTPSettingsExtension
    server = ServerSettings
    sounds = SoundSettings
    tls = TLSSettingsExtension

    user_agent = Setting(type=str, default='Blink %s (%s)' % (__version__, platform.system() if sys.platform!='darwin' else 'MacOSX Qt'))


class BlinkPresenceSettings(SettingsGroup):
    current_state = Setting(type=PresenceState, default=PresenceState('Available'))
    state_history = Setting(type=PresenceStateList, default=PresenceStateList())
    icon = Setting(type=IconDescriptor, nillable=True)


class BlinkSettings(SettingsObject):
    __id__ = 'BlinkSettings'

    presence = BlinkPresenceSettings

