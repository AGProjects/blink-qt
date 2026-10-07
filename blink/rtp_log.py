"""RTP media: what Blink for macOS shows in the RTP tab of its debug window.

Per call: the RTP endpoints (with the ICE candidate types), the codec, the
encryption in use and the account's encryption setting as stored, the codecs
we proposed, the negotiated video fmtp, the X-Sylk-ZRTP capabilities and the
remote user agent; then ICE negotiation, RTP parameter changes, SRTP/ZRTP
encryption and Sylk-ZRTP state as they happen.

Lines go to the RTP tab of the logs window (UILogMessage, section 'rtp') and
to logs/rtp_trace.txt. Ported from DebugWindow.py (renderAudio, renderVideo and
the RTPStream* handlers); the wording is kept so logs from both clients read
the same. The echo canceller statistics (sipsimple AudioMixer.ec_statistics)
are read during audio calls and once more when one ends, as macOS does.
"""

import os
import threading

from datetime import datetime

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.configuration.settings import SIPSimpleSettings

from blink.logging import LogFile
from blink.resources import ApplicationData
from blink.util import call_in_gui_thread, call_later


__all__ = ['RTPLog']


ice_candidates = {'srflx': 'Server Reflexive', 'prflx': 'Peer Reflexive', 'host': 'Host', 'relay': 'Server Relay'}


def _text(value):
    if isinstance(value, (bytes, bytearray)):
        return value.decode('utf-8', 'replace')
    return value


def _candidate_type(candidate):
    kind = str(getattr(candidate, 'type', '') or '').lower()
    return ice_candidates.get(kind, kind or '?')


@implementer(IObserver)
class RTPLog(object, metaclass=Singleton):

    # echo canceller statistics: first read once the canceller has had time to converge, then periodically
    ec_first_delay = 10
    ec_interval = 30
    # highlighted as audible echo: little speaker-to-mic isolation (ERL) and little removed by the
    # linear filter (ERLE) after converging (thresholds from the AEC harness in python3-sipsimple)
    EC_SUSPECT_ERL_DB = 12
    EC_SUSPECT_ERLE_DB = 10
    EC_SUSPECT_MIN_DURATION = 5

    notifications = ('SIPSessionDidEnd', 'SIPSessionDidFail', 'SIPSessionDidStart', 'SIPSessionDidRenegotiateStreams', 'RTPStreamDidChangeRTPParameters',
                     'RTPStreamICENegotiationDidSucceed', 'RTPStreamICENegotiationDidFail', 'RTPStreamICENegotiationStateDidChange',
                     'RTPStreamDidEnableEncryption', 'RTPStreamDidNotEnableEncryption', 'RTPStreamZRTPReceivedSAS',
                     'RTPStreamZRTPVerifiedStateChanged', 'RTPStreamZRTPLog', 'RTPStreamZRTPPeerNameChanged',
                     'SIPSessionSylkZRTPStateChanged')

    def __init__(self):
        self._started = False
        self._lock = threading.Lock()
        self._file = None
        self._audio_sessions = set()
        self._ec_timer_armed = False

    def start(self):
        if self._started:
            return
        self._started = True
        self._file = LogFile(os.path.join(ApplicationData.directory, 'logs', 'rtp_trace.txt'))
        notification_center = NotificationCenter()
        for name in self.notifications:
            notification_center.add_observer(self, name=name)

    def stop(self):
        if not self._started:
            return
        self._started = False
        notification_center = NotificationCenter()
        for name in self.notifications:
            notification_center.discard_observer(self, name=name)
        with self._lock:
            self._file.close()

    # output

    def write(self, text):
        text = text.rstrip('\n')
        if not text:
            return
        with self._lock:
            try:
                self._file.write(text + '\n')
                self._file.flush()
            except (OSError, AttributeError):
                pass
        NotificationCenter().post_notification('UILogMessage', data=NotificationData(message=text, section='rtp'))

    # notifications

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        try:
            handler(notification)
        except Exception as e:
            self.write('%s Cannot log %s: %r' % (datetime.now(), notification.name, e))

    def _NH_SIPSessionDidStart(self, notification):
        self.render(notification.sender)
        self._watch_echo_canceller(notification.sender)

    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        if notification.data.added_streams:
            self.render(notification.sender)
            self._watch_echo_canceller(notification.sender)

    def _NH_SIPSessionDidEnd(self, notification):
        self._end_echo_canceller(notification.sender)

    def _NH_SIPSessionDidFail(self, notification):
        self._end_echo_canceller(notification.sender)

    # echo canceller

    def _watch_echo_canceller(self, session):
        if not any(stream.type == 'audio' for stream in session.streams or []):
            return
        with self._lock:
            self._audio_sessions.add(session)
            arm = not self._ec_timer_armed
            self._ec_timer_armed = True
        if arm:
            call_in_gui_thread(call_later, self.ec_first_delay, self._ec_tick)

    def _end_echo_canceller(self, session):
        with self._lock:
            if session not in self._audio_sessions:
                return
            self._audio_sessions.discard(session)
        call_in_gui_thread(self._log_echo_canceller, session, True)

    def _ec_tick(self):
        with self._lock:
            sessions = list(self._audio_sessions)
            self._ec_timer_armed = bool(sessions)
        if not sessions:
            return
        for session in sessions:
            self._log_echo_canceller(session, False)
        call_later(self.ec_interval, self._ec_tick)

    def _log_echo_canceller(self, session, final):
        from sipsimple.application import SIPApplication
        try:
            mixer = SIPApplication.voice_audio_mixer
            stats = mixer.ec_statistics if mixer is not None else None
        except Exception as e:
            self.write('%s Audio echo canceller: statistics not available (%s)' % (datetime.now(), e))
            return
        if not stats:
            return      # no software canceller on this device, or under a second of audio
        uri = getattr(getattr(session, 'remote_identity', None), 'uri', None)
        target = str(uri).partition(':')[2] if uri is not None else None
        summary = stats.get('info') or ', '.join('%s=%s' % item for item in sorted(stats.items()) if item[1] is not None)
        text = '%s Audio echo canceller%s%s: %s' % (datetime.now(), ' at end of call' if final else '', (' to %s' % target) if target else '', summary)
        erl, erle, duration = stats.get('erl'), stats.get('erle'), stats.get('duration')
        if (erl is not None and erle is not None and duration is not None and duration >= self.EC_SUSPECT_MIN_DURATION
                and erl < self.EC_SUSPECT_ERL_DB and erle < self.EC_SUSPECT_ERLE_DB):
            text += '  <- echo may be audible (low ERL and ERLE)'
        self.write(text)

    def render(self, session):
        for media in ('audio', 'video'):
            stream = next((stream for stream in session.streams or [] if stream.type == media), None)
            if stream is not None:
                self.write(self._render_stream(session, stream))

    def _render_stream(self, session, stream):
        when = session.start_time
        media = stream.type.capitalize()
        text = '\n%s New %s call %s\n' % (when, media, session.remote_identity)
        if stream.local_rtp_address and stream.local_rtp_port and stream.remote_rtp_address and stream.remote_rtp_port:
            if stream.ice_active and stream.local_rtp_candidate and stream.remote_rtp_candidate:
                text += '%s %s RTP endpoints %s:%d (ICE type %s) <-> %s:%d (ICE type %s)\n' % (when, media,
                                                                                                stream.local_rtp_address, stream.local_rtp_port, _candidate_type(stream.local_rtp_candidate),
                                                                                                stream.remote_rtp_address, stream.remote_rtp_port, _candidate_type(stream.remote_rtp_candidate))
            else:
                text += '%s %s RTP endpoints %s:%d <-> %s:%d\n' % (when, media, stream.local_rtp_address, stream.local_rtp_port, stream.remote_rtp_address, stream.remote_rtp_port)
        if stream.codec and stream.sample_rate:
            text += '%s %s call established using %s codec at %sHz\n' % (when, media, _text(stream.codec), stream.sample_rate)
        if stream.encryption.active:
            text += '%s RTP %s stream is encrypted with %s (%s)\n' % (when, stream.type, stream.encryption.type, _text(stream.encryption.cipher))
        # the account's setting verbatim: the preferences label can read "disabled" for a stored sdes_optional
        account = getattr(session, 'account', None)
        if account is not None:
            encryption = getattr(getattr(account, 'rtp', None), 'encryption', None)
            if encryption is None:
                text += '%s %s RTP encryption setting: account has no rtp.encryption (BonjourAccount?)\n' % (when, media)
            else:
                text += '%s %s RTP encryption setting: enabled=%s, key_negotiation=%s\n' % (when, media, getattr(encryption, 'enabled', False), getattr(encryption, 'key_negotiation', None) or '(unset)')
        # what we offered, against the negotiated codec above
        settings = SIPSimpleSettings()
        if stream.type == 'audio':
            codecs = list(settings.rtp.audio_codec_list or [])
            if codecs:
                text += '%s Proposed audio codecs: %s\n' % (when, ', '.join(codecs))
        else:
            codecs = list(settings.rtp.video_codec_list or [])
            if codecs:
                text += '%s Proposed video codecs: %s\n' % (when, ', '.join(codecs))
            h264 = getattr(settings.video, 'h264', None)
            if h264 is not None:
                resolution = settings.video.resolution
                max_bitrate = settings.video.max_bitrate
                text += '%s Proposed video options: H.264 profile=%s level=%s, resolution=%sx%s, framerate=%s fps, max_bitrate=%s\n' % (
                    when, getattr(h264, 'profile', '?'), getattr(h264, 'level', '?'),
                    resolution[0] if resolution else '?', resolution[1] if resolution else '?',
                    settings.video.framerate, ('%s Mbps' % max_bitrate) if max_bitrate else 'auto')
            try:
                fmtp = self._video_negotiated_fmtp(session)
            except Exception as e:
                text += '%s Video codec fmtp: error parsing local SDP (%s)\n' % (when, e)
            else:
                if fmtp is not None:
                    text += '%s %s\n' % (when, fmtp)
        local_capability, remote_capability = self._sylk_zrtp_capabilities(session)
        if local_capability is not None:
            text += '%s Local X-Sylk-ZRTP capability advertised: %s\n' % (when, local_capability)
        if remote_capability is not None:
            text += '%s Remote X-Sylk-ZRTP capability detected: %s\n' % (when, remote_capability)
        if session.remote_user_agent is not None:
            text += '%s Remote SIP User Agent is "%s"\n' % (when, session.remote_user_agent)
        return text

    @staticmethod
    def _sylk_zrtp_capabilities(session):
        local = None
        account = getattr(session, 'account', None)
        encryption = getattr(getattr(account, 'rtp', None), 'encryption', None)
        if encryption is not None and getattr(encryption, 'enabled', False) and encryption.key_negotiation in ('opportunistic', 'zrtp'):
            local = 'v=1; suites=AES-128-GCM'
        remote = None
        for attribute in ('remote_request_headers', 'remote_response_headers'):
            headers = getattr(session, attribute, None)
            if not headers:
                continue
            try:
                header = headers.get('X-Sylk-ZRTP') or headers.get('x-sylk-zrtp')
            except Exception:
                header = None
            if header is not None:
                remote = _text(header.body if hasattr(header, 'body') else str(header))
                break
        return local, remote

    @staticmethod
    def _video_negotiated_fmtp(session):
        """'Video codec negotiated: <rtpmap> (PT n, from <where>) / fmtp: <fmtp>' from the SDP we send, or None."""
        sdp = source = None
        invitation_sdp = getattr(getattr(session, '_invitation', None), 'sdp', None)
        if invitation_sdp is not None:
            for name in ('active_local', 'proposed_local'):
                candidate = getattr(invitation_sdp, name, None)
                if candidate is not None and getattr(candidate, 'media', None):
                    sdp, source = candidate, 'invitation.sdp.' + name
                    break
        if sdp is None:
            stream = next((stream for stream in session.streams or [] if stream.type == 'video'), None)
            for name in ('_local_sdp', 'local_sdp'):
                candidate = getattr(stream, name, None)
                if candidate is not None and getattr(candidate, 'media', None):
                    sdp, source = candidate, 'stream.' + name
                    break
        if sdp is None:
            return None
        video = next((media for media in sdp.media if _text(media.media) == 'video'), None)
        if video is None or not getattr(video, 'formats', None):
            return None
        payload_type = str(_text(video.formats[0]))
        rtpmap = fmtp = None
        for attribute in video.attributes:
            name, value = _text(attribute.name), _text(attribute.value) or ''
            if name == 'rtpmap' and value.startswith(payload_type + ' '):
                rtpmap = value.partition(' ')[2]
            elif name == 'fmtp' and value.startswith(payload_type + ' '):
                fmtp = value.partition(' ')[2]
        head = ('Video codec negotiated: %s (PT %s, from %s)' % (rtpmap, payload_type, source)) if rtpmap else ('Video codec negotiated: PT %s (no rtpmap, from %s)' % (payload_type, source))
        return '%s / fmtp: %s' % (head, fmtp or '(none)')

    def _NH_RTPStreamDidChangeRTPParameters(self, notification):
        stream = notification.sender
        media = stream.type.upper()
        text = '%s %s call to %s: RTP parameters changed\n' % (notification.datetime, media, stream.session.remote_identity)
        if stream.local_rtp_address and stream.local_rtp_port and stream.remote_rtp_address and stream.remote_rtp_port:
            text += '%s %s RTP endpoints %s:%d <-> %s:%d\n' % (notification.datetime, media, stream.local_rtp_address, stream.local_rtp_port, stream.remote_rtp_address, stream.remote_rtp_port)
        if stream.codec and stream.sample_rate:
            text += '%s %s call established using %s codec at %sHz\n' % (notification.datetime, media, _text(stream.codec), stream.sample_rate)
        self.write(text)

    def _NH_RTPStreamICENegotiationDidSucceed(self, notification):
        data = notification.data
        stream = notification.sender
        media = stream.type.upper()
        text = '%s %s call %s, ICE negotiation succeeded in %s\n' % (notification.datetime, media, stream.session.remote_identity, data.duration)
        if stream.local_rtp_candidate and stream.remote_rtp_candidate:
            text += '%s %s RTP endpoints: %s:%d (ICE type %s) <-> %s:%d (ICE type %s)\n' % (notification.datetime, media,
                                                                                             stream.local_rtp_address, stream.local_rtp_port, str(stream.local_rtp_candidate.type).lower(),
                                                                                             stream.remote_rtp_address, stream.remote_rtp_port, str(stream.remote_rtp_candidate.type).lower())
        text += '%s Local ICE candidates:\n' % media
        text += ''.join('\t%s\n' % candidate for candidate in data.local_candidates)
        text += '%s Remote ICE candidates:\n' % media
        text += ''.join('\t%s\n' % candidate for candidate in data.remote_candidates)
        text += '%s ICE connectivity checks results:\n' % media
        text += ''.join('\t%s\n' % check for check in data.valid_list)
        self.write(text)

    def _NH_RTPStreamICENegotiationStateDidChange(self, notification):
        # macOS assigns these to the wrong variable and so logs only GATHERING; all of them here
        media = notification.sender.type.upper()
        text = {'GATHERING': 'ICE %s gathering candidates ...',
                'NEGOTIATION_START': 'Connecting ICE %s ...',
                'NEGOTIATING': 'Negotiating ICE %s ...',
                'GATHERING_COMPLETE': 'ICE %s gathering candidates complete',
                'RUNNING': 'ICE %s negotiation succeeded',
                'FAILED': 'ICE %s negotiation failed'}.get(notification.data.state)
        if text:
            self.write('%s %s' % (notification.datetime, text % media))

    def _NH_RTPStreamICENegotiationDidFail(self, notification):
        self.write('%s %s ICE negotiation failed: %s' % (notification.datetime, notification.sender.type.upper(), _text(notification.data.reason)))

    def _NH_RTPStreamDidEnableEncryption(self, notification):
        stream = notification.sender
        media = stream.type.upper()
        encryption = getattr(stream, 'encryption', None)
        kind = getattr(encryption, 'type', '?') if encryption else '?'
        cipher = _text(getattr(encryption, 'cipher', '?') if encryption else '?')
        self.write('%s %s encryption active: %s / %s' % (notification.datetime, media, kind, cipher))
        if kind == 'ZRTP':
            zrtp = getattr(encryption, 'zrtp', None)
            if zrtp is not None:
                self.write('%s %s ZRTP peer is %s (peer name: %s)' % (notification.datetime, media, 'verified' if getattr(zrtp, 'verified', False) else 'not yet verified', getattr(zrtp, 'peer_name', None) or '<not set>'))

    def _NH_RTPStreamDidNotEnableEncryption(self, notification):
        self.write('%s %s encryption NOT enabled: %s' % (notification.datetime, notification.sender.type.upper(), _text(getattr(notification.data, 'reason', '<unknown>'))))

    def _NH_RTPStreamZRTPReceivedSAS(self, notification):
        data = notification.data
        self.write('%s %s ZRTP SAS received: %s (peer %s, name: %s)' % (notification.datetime, notification.sender.type.upper(), getattr(data, 'sas', '?'),
                                                                        'verified' if getattr(data, 'verified', False) else 'not verified', getattr(data, 'peer_name', None) or '<not set>'))

    def _NH_RTPStreamZRTPVerifiedStateChanged(self, notification):
        self.write('%s %s ZRTP peer marked as %s' % (notification.datetime, notification.sender.type.upper(), 'verified' if getattr(notification.data, 'verified', False) else 'NOT verified'))

    def _NH_RTPStreamZRTPPeerNameChanged(self, notification):
        self.write('%s %s ZRTP peer name set to %s' % (notification.datetime, notification.sender.type.upper(), getattr(notification.data, 'name', '') or '<empty>'))

    def _NH_RTPStreamZRTPLog(self, notification):
        data = notification.data
        message = _text(getattr(data, 'message', None) or getattr(data, 'log', '')) or str(getattr(data, 'text', '') or getattr(data, 'data', '') or '')
        self.write('%s %s ZRTP [%s] %s' % (notification.datetime, notification.sender.type.upper(), getattr(data, 'level', ''), message))

    def _NH_SIPSessionSylkZRTPStateChanged(self, notification):
        data = notification.data
        state = getattr(data, 'state', None)
        when = notification.datetime
        if state == 'probing':
            self.write('%s Sylk-ZRTP handshake started (role=%s)' % (when, getattr(data, 'role', '?')))
        elif state == 'key-agreed':
            sas = getattr(data, 'sas', None)
            self.write('%s Sylk-ZRTP key agreed%s' % (when, ' — SAS: %s' % sas if sas else ''))
        elif state == 'key-active':
            installed = []
            for entry in getattr(data, 'installed_streams', None) or []:
                try:
                    kind, codec, prefix = entry
                    installed.append('%s(%s,prefix=%d)' % (kind, codec, prefix))
                except (TypeError, ValueError):
                    installed.append(repr(entry))
            self.write('%s Sylk-ZRTP active — media end-to-end encrypted (AES-128-GCM)%s' % (when, ' — installed on: %s' % ', '.join(installed) if installed else ''))
            for entry in getattr(data, 'failed_streams', None) or []:
                try:
                    kind, codec, reason = entry
                    self.write('%s Sylk-ZRTP note — %s stream stayed plain (codec=%s): %s' % (when, kind, codec, reason))
                except (TypeError, ValueError):
                    self.write('%s Sylk-ZRTP note — stream stayed plain: %r' % (when, entry))
        elif state == 'failed':
            self.write('%s Sylk-ZRTP handshake failed: %s' % (when, getattr(data, 'error', None) or getattr(data, 'reason', '') or '<unknown>'))
            for entry in getattr(data, 'failed_streams', None) or []:
                try:
                    kind, codec, reason = entry
                    self.write('%s     %s stream (codec=%s) — %s' % (when, kind, codec, reason))
                except (TypeError, ValueError):
                    pass
