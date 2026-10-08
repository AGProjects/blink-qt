
"""Blink settings extensions."""

__all__ = ['BlinkSettings', 'SIPSimpleSettingsExtension', 'H264_LEVELS']

import os
import platform
import sys

from sipsimple.configuration import Setting, SettingsGroup, SettingsObject, SettingsObjectExtension
from sipsimple.configuration.datatypes import AudioCodecList, H264Profile, NonNegativeInteger, PositiveInteger, Path, SampleRate, VideoCodecList, VideoResolution
from sipsimple.configuration.settings import AudioSettings, ChatSettings, EchoCancellerSettings, H264Settings, LogsSettings, RTPSettings, SIPSettings, TLSSettings, VideoSettings

from blink import __version__
from blink.configuration.datatypes import ApplicationDataPath, GraphTimeScale, HTTPURL, IconDescriptor, SoundFile, PresenceState, PresenceStateList
from blink.resources import Resources

try:
    from blink.configuration._codecs import RTPSettingsExtension
except ImportError:
    class RTPSettingsExtension(RTPSettings):
        # G729 is in the core since SDK 5.x
        audio_codec_order = Setting(type=AudioCodecList, default=AudioCodecList(('opus', 'G722', 'G729', 'PCMU', 'PCMA')))
        audio_codec_list = Setting(type=AudioCodecList, default=AudioCodecList(('opus', 'G722', 'G729', 'PCMU', 'PCMA')))
        video_codec_order = Setting(type=VideoCodecList, default=VideoCodecList(('H264', 'VP8', 'VP9')))


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


# Video: VGA, constrained baseline H.264 at level 3.0 (the level for 640x480, so the SDP
# offers what is sent; baseline is what WebRTC peers decode) and 0.8 Mbps, which a normal
# 4G or weak WiFi uplink sustains.

# The H.264 level for each resolution, so the SDP offers what is sent
H264_LEVELS = {'320x240': '2.0', '640x360': '3.0', '640x480': '3.0', '960x540': '3.1', '1280x720': '3.1', '1920x1080': '4.0'}


class H264SettingsExtension(H264Settings):
    profile = Setting(type=H264Profile, default='baseline')
    level = Setting(type=str, default='3.0')      # follows the resolution, see H264_LEVELS


class VideoSettingsExtension(VideoSettings):
    resolution = Setting(type=VideoResolution, default=VideoResolution('640x480'))
    framerate = Setting(type=int, default=24)
    max_bitrate = Setting(type=float, default=0.8, nillable=True)   # Mbps; None is automatic
    h264 = H264SettingsExtension


class SIPSettingsExtension(SIPSettings):
    auto_answer_interval = Setting(type=int, default=15)
    auto_answer = Setting(type=bool, default=True)
    auto_record = Setting(type=bool, default=False)


class ChatSettingsExtension(ChatSettings):
    auto_accept = Setting(type=bool, default=False)
    sms_replication = Setting(type=bool, default=True)
    history_directory = Setting(type=ApplicationDataPath, default=ApplicationDataPath('history'))
    keys_directory = Setting(type=ApplicationDataPath, default=ApplicationDataPath('keys'))


class GoogleContactsSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    username = Setting(type=str, default=None, nillable=True)


class LogsSettingsExtension(LogsSettings):
    trace_sip = Setting(type=bool, default=False)
    trace_pjsip = Setting(type=bool, default=False)
    trace_messaging = Setting(type=bool, default=False)
    trace_msrp = Setting(type=bool, default=False)
    trace_xcap = Setting(type=bool, default=False)
    trace_notifications = Setting(type=bool, default=False)
    activity_debug = Setting(type=bool, default=False)


class ServerSettings(SettingsGroup):
    enrollment_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/enrollment.phtml")
    updater_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/BlinkQTAppcast.xml")


class SoundSettings(SettingsGroup):
    inbound_ringtone = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/inbound_ringtone.wav')), nillable=True)
    outbound_ringtone = Setting(type=SoundFile, default=SoundFile(Resources.get('sounds/outbound_ringtone.wav')), nillable=True)
    play_message_alerts = Setting(type=bool, default=True)
    play_message_read_sound = Setting(type=bool, default=True)   # when the open conversation's peer reads a message


# The system CA bundle is kept current by the distribution; the bundled one is
# not (it still has the expired DST Root CA X3 chain and lacks the current
# Let's Encrypt roots), so it is only the fallback.
_system_ca_bundles = ('/etc/ssl/certs/ca-certificates.crt',     # Debian, Ubuntu
                      '/etc/pki/tls/certs/ca-bundle.crt',       # Fedora, RHEL
                      '/etc/ssl/ca-bundle.pem',                 # openSUSE
                      '/etc/ssl/cert.pem')                      # macOS, BSD, Alpine
_default_ca_list = next((path for path in _system_ca_bundles if os.path.isfile(path)), Resources.get('tls/ca.crt'))

try:
    from blink.configuration._tls import TLSSettingsExtension
except ImportError:
    class TLSSettingsExtension(TLSSettings):
        ca_list = Setting(type=ApplicationDataPath, default=ApplicationDataPath(_default_ca_list), nillable=True)
        certificate = Setting(type=ApplicationDataPath, default=ApplicationDataPath(Resources.get('tls/default.crt')), nillable=True)
        verify_server = Setting(type=bool, default=True)


class SIPSimpleSettingsExtension(SettingsObjectExtension):
    answering_machine = AnsweringMachineSettings
    audio = AudioSettingsExtension
    chat = ChatSettingsExtension
    google_contacts = GoogleContactsSettings
    logs = LogsSettingsExtension
    rtp = RTPSettingsExtension
    server = ServerSettings
    sounds = SoundSettings
    tls = TLSSettingsExtension
    sip = SIPSettingsExtension
    video = VideoSettingsExtension

    user_agent = Setting(type=str, default='Blink %s (%s)' % (__version__, platform.system() if sys.platform != 'darwin' else 'MacOSX Qt'))


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
    scale = Setting(type=bool, default=True)
    open_fullscreen = Setting(type=bool, default=False)
    open_viewonly = Setting(type=bool, default=False)


class BlinkPresenceSettings(SettingsGroup):
    current_state = Setting(type=PresenceState, default=PresenceState('Available'))
    state_history = Setting(type=PresenceStateList, default=PresenceStateList())
    offline_note = Setting(type=str, nillable=True)
    icon = Setting(type=IconDescriptor, nillable=True)


class BlinkInterfaceSettings(SettingsGroup):
    show_history_name_and_uri = Setting(type=bool, default=False)
    language = Setting(type=str, default='default')
    show_messages_group = Setting(type=bool, default=True)


class BlinkSettings(SettingsObject):
    __id__ = 'BlinkSettings'

    chat_window = ChatWindowSettings
    presence = BlinkPresenceSettings
    screen_sharing = BlinkScreenSharingSettings
    interface = BlinkInterfaceSettings

    video_settings_version = Setting(type=int, default=0)   # see Blink._migrate_video_settings
    screenshots_directory = Setting(type=Path, default=Path('~/Downloads'))
    transfers_directory = Setting(type=Path, default=Path('~/Downloads'))

