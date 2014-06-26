# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Blink settings extensions."""

__all__ = ['BlinkSettings', 'SIPSimpleSettingsExtension']

import os
import platform
import sys

from sipsimple.configuration import Setting, SettingsGroup, SettingsObject, SettingsObjectExtension
from sipsimple.configuration.datatypes import AudioCodecList, NonNegativeInteger, PositiveInteger, Path, SampleRate
from sipsimple.configuration.settings import AudioSettings, ChatSettings, EchoCancellerSettings, FileTransferSettings, LogsSettings, RTPSettings, TLSSettings

from blink import __version__
from blink.configuration.datatypes import ApplicationDataPath, AuthorizationToken, GraphTimeScale, HTTPURL, IconDescriptor, SoundFile, PresenceState, PresenceStateList
from blink.resources import Resources


class AnsweringMachineSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    answer_delay = Setting(type=NonNegativeInteger, default=10)
    max_recording = Setting(type=PositiveInteger, default=3)
    unavailable_message = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/unavailable_message.wav')), nillable=True)


class EchoCancellerSettingsExtension(EchoCancellerSettings):
    enabled = Setting(type=bool, default=True)
    tail_length = Setting(type=NonNegativeInteger, default=2)


class AudioSettingsExtension(AudioSettings):
    recordings_directory = Setting(type=ApplicationDataPath, default=ApplicationDataPath('recordings'))
    sample_rate = Setting(type=SampleRate, default=32000)
    echo_canceller = EchoCancellerSettingsExtension


class ChatSettingsExtension(ChatSettings):
    auto_accept = Setting(type=bool, default=False)
    sms_replication = Setting(type=bool, default=True)
    history_directory = Setting(type=ApplicationDataPath, default=ApplicationDataPath('history'))


class FileTransferSettingsExtension(FileTransferSettings):
    directory = Setting(type=Path, default=Path(os.path.expanduser('~/Downloads')))


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
    audio_codec_order = Setting(type=AudioCodecList, default=AudioCodecList(('opus', 'G722', 'speex', 'GSM', 'iLBC', 'PCMU', 'PCMA')))


class ServerSettings(SettingsGroup):
    enrollment_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/enrollment.phtml")
    updater_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/BlinkQTAppcast.xml")


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
    ca_list = Setting(type=ApplicationDataPath, default=ApplicationDataPath(Resources.get('tls/ca.crt')), nillable=True)


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


class SessionInfoSettings(SettingsGroup):
    alternate_style = Setting(type=bool, default=False)
    bytes_per_second = Setting(type=bool, default=False)
    graph_time_scale = Setting(type=GraphTimeScale, default=3)


class ChatWindowSettings(SettingsGroup):
    session_info = SessionInfoSettings

    style = Setting(type=str, default='Stockholm')
    style_variant = Setting(type=str, default=None, nillable=True)
    show_user_icons = Setting(type=bool, default=True)
    font = Setting(type=str, default=None, nillable=True)
    font_size = Setting(type=int, default=None, nillable=True)


class BlinkScreenSharingSettings(SettingsGroup):
    screenshots_directory = Setting(type=Path, default=Path(os.path.expanduser('~/Downloads')))
    scale = Setting(type=bool, default=True)
    open_fullscreen = Setting(type=bool, default=False)
    open_viewonly = Setting(type=bool, default=False)


class BlinkPresenceSettings(SettingsGroup):
    current_state = Setting(type=PresenceState, default=PresenceState('Available'))
    state_history = Setting(type=PresenceStateList, default=PresenceStateList())
    offline_note = Setting(type=unicode, nillable=True)
    icon = Setting(type=IconDescriptor, nillable=True)


class BlinkSettings(SettingsObject):
    __id__ = 'BlinkSettings'

    chat_window = ChatWindowSettings
    presence = BlinkPresenceSettings
    screen_sharing = BlinkScreenSharingSettings

