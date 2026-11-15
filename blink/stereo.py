"""Stereo audio calls (Preferences, Audio: Enable Stereo Mode; the contact menus' Start Stereo Audio Call).

A stereo call needs the SDK in stereo (python3-sipsimple 5.7, audio.stereo): a stereo mixer, Opus in stereo, and no
echo canceller (it is mono and made for speech). It is switched on for the call, before the session is made (the
mixer is made again: the session waits for it), and switched back off when no stereo call is left, unless something
else wants it on (AG CuteMusic's mixer as the device, blink/cutemusic.py: its music mode).

The call's audio stream is AudioStream(stereo=True): it offers only the codecs that carry stereo (Opus).

Incoming: an offer asking for stereo Opus (AudioStream.offered_stereo) switches the SDK to stereo while the call
rings, so that it is answered in stereo; the stream moves to the stereo mixer when it is accepted (the SDK). Only
when no other audio call is in progress: making the mixer again under a call in progress would take its audio away.
"""

from PyQt6.QtCore import QObject, QTimer

from sipsimple.configuration.settings import SIPSimpleSettings

from blink.logging import ActivityLog
from blink.util import QSingleton


__all__ = ['StereoCalls', 'stereo_available', 'contact_wants_stereo']


def stereo_available():
    """Whether stereo calls are offered: enabled in the preferences, and an SDK that has stereo audio."""
    settings = SIPSimpleSettings()
    return bool(getattr(settings.audio, 'enable_stereo', False)) and hasattr(settings.audio, 'stereo')


def contact_wants_stereo(contact):
    """Whether audio calls to contact are made in stereo: Enable Stereo Mode on, and the contact's own Enable Stereo
    (shared over XCAP with Blink for macOS)."""
    if not stereo_available() or getattr(contact, 'type', None) != 'addressbook':
        return False
    return bool(getattr(getattr(contact, 'settings', None), 'stereo', False))


class StereoCalls(QObject, metaclass=QSingleton):
    """Starts stereo calls and puts the SDK back in mono after the last one."""

    MIXER_DELAY = 600       # ms: the session made after the stereo mixer

    def __init__(self):
        super().__init__()
        self._sessions = []         # the stereo calls made, while they last
        self._incoming = []         # the stereo offers answered in stereo (sipsimple sessions), while they last
        self._engaged = False       # the SDK put in stereo here (to be put back)
        self._timer = QTimer(self, interval=2000)
        self._timer.timeout.connect(self._check)

    def start(self, contact, contact_uri, account=None, streams=None, on_session=None):
        """A stereo call: streams as create_session takes them (default: audio only), the audio made stereo;
        on_session(session) once the session is made (None when it was not)."""
        from blink.sessions import SessionManager, StreamDescription
        settings = SIPSimpleSettings()
        delay = 0
        if not settings.audio.stereo:
            settings.audio.stereo = True
            settings.save()
            self._engaged = True
            delay = self.MIXER_DELAY
            ActivityLog().info('[audio] Stereo call: the audio in stereo (Opus stereo, no echo cancellation)')

        def call():
            descriptions = [StreamDescription('audio', stereo=True) if stream.type == 'audio' else stream
                            for stream in (streams or [StreamDescription('audio')])]
            session = SessionManager().create_session(contact, contact_uri, descriptions, account=account)
            if session is not None:
                self._sessions.append(session)
                self._timer.start()
            if on_session is not None:
                on_session(session)
        QTimer.singleShot(delay, call)

    def incoming(self, session, audio_stream):
        """A new incoming session: answered in stereo if it asks for stereo Opus and stereo calls are enabled."""
        if not stereo_available() or not getattr(audio_stream, 'offered_stereo', False):
            return
        settings = SIPSimpleSettings()
        if not settings.audio.stereo:
            from blink.sessions import SessionManager
            others = [blink_session for blink_session in SessionManager().sessions
                      if blink_session.sip_session is not session and 'audio' in blink_session.streams
                      and str(getattr(blink_session, 'state', '') or '').startswith(('connecting', 'connected'))]
            if others:
                ActivityLog().info('[audio] Stereo offer answered in mono: another audio call is in progress')
                return
            settings.audio.stereo = True
            settings.save()
            self._engaged = True
            ActivityLog().info('[audio] Incoming stereo offer: the audio in stereo (Opus stereo, no echo cancellation)')
        self._incoming.append(session)
        self._timer.start()

    def _check(self):
        self._sessions = [session for session in self._sessions
                          if str(getattr(session, 'state', '') or '').startswith(('connecting', 'connected'))
                          or getattr(session, 'state', None) in ('initialized', 'initializing')]
        self._incoming = [session for session in self._incoming if getattr(session, 'state', None) not in (None, 'terminating', 'terminated')]
        if self._sessions or self._incoming:
            return
        self._timer.stop()
        if not self._engaged:
            return
        from blink import cutemusic
        if cutemusic.selected():        # AG CuteMusic's mixer as the device: its music mode keeps stereo
            self._engaged = False
            return
        settings = SIPSimpleSettings()
        if settings.audio.stereo:
            settings.audio.stereo = False
            settings.save()
            ActivityLog().info('[audio] No stereo call left: the audio in mono again')
        self._engaged = False
