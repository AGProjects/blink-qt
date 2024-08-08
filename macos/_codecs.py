from sipsimple.configuration.datatypes import AudioCodecList, VideoCodecList
from sipsimple.configuration.settings import RTPSettings, Setting

class RTPSettingsExtension(RTPSettings):
    audio_codec_order = Setting(type=AudioCodecList, default=AudioCodecList(('opus', 'G722', 'PCMU', 'PCMA')))
    video_codec_order = Setting(type=VideoCodecList, default=VideoCodecList(('H264', 'VP8', 'VP9')))

