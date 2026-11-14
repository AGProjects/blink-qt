"""Stereo audio calls (Preferences, Audio: Enable Stereo Mode; the contact menus' Start Stereo Audio Call).

A stereo call needs the SDK in stereo (python3-sipsimple 5.7, audio.stereo): a stereo mixer, Opus in stereo, and no
echo canceller (it is mono and made for speech). It is switched on for the call, before the session is made (the
mixer is made again: the session waits for it), and switched back off when no stereo call is left, unless something
else wants it on (AG CuteMusic's mixer as the device, blink/cutemusic.py: its music mode).

The call's audio stream is AudioStream(stereo=True): it offers only the codecs that carry stereo (Opus).
"""

from PyQt6.QtCore import QObject, QTimer

from sipsimple.configuration.settings import SIPSimpleSettings

from blink.logging import ActivityLog
from blink.util import QSingleton


__all__ = ['StereoCalls', 'stereo_available']


def stereo_available():
    """Whether stereo calls are offered: enabled in the preferences, and an SDK that has stereo audio."""
    settings = SIPSimpleSettings()
    return bool(getattr(settings.audio, 'enable_stereo', False)) and hasattr(settings.audio, 'stereo')


class StereoCalls(QObject, metaclass=QSingleton):
    """Starts stereo calls and puts the SDK back in mono after the last one."""

    MIXER_DELAY = 600       # ms: the session made after the stereo mixer

    def __init__(self):
        super().__init__()
        self._sessions = []         # the stereo calls made, while they last
        self._engaged = False       # the SDK put in stereo here (to be put back)
        self._timer = QTimer(self, interval=2000)
        self._timer.timeout.connect(self._check)

    def start(self, contact, contact_uri, account=None):
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
            session = SessionManager().create_session(contact, contact_uri, [StreamDescription('audio', stereo=True)], account=account)
            if session is not None:
                self._sessions.append(session)
                self._timer.start()
        QTimer.singleShot(delay, call)

    def _check(self):
        self._sessions = [session for session in self._sessions
                          if str(getattr(session, 'state', '') or '').startswith(('connecting', 'connected'))
                          or getattr(session, 'state', None) in ('initialized', 'initializing')]
        if self._sessions:
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
