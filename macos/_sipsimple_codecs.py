from sipsimple.configuration import Setting, SettingsGroup
from sipsimple.configuration.datatypes import AudioCodecList, VideoCodecList
from sipsimple.configuration.datatypes import Port, PortRange, NonNegativeInteger

class RTPSettings(SettingsGroup):
    port_range = Setting(type=PortRange, default=PortRange(50000, 50500))
    timeout = Setting(type=NonNegativeInteger, default=30)
    audio_codec_list = Setting(type=AudioCodecList, default=AudioCodecList(('opus', 'G722', 'PCMU', 'PCMA')))
    video_codec_list = Setting(type=VideoCodecList, default=VideoCodecList(('H264', 'VP8', 'VP9')))

