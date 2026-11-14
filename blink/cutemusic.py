"""AG CuteMusic's mixer as a sound device: "AG Podcast Mixer" (docs/jamming-ag-cutemusic.md).

AG CuteMusic, while it runs, makes the sound server devices behind an ALSA device of that name (its package's
/etc/alsa/conf.d/60-ag-cute-music.conf): what Blink records from it is the karaoke (the singer, and the music), what
Blink plays on it goes to AG CuteMusic's mixer, heard in the singer's headset.

- The device is listed (Devices menu, Preferences) only while AG CuteMusic runs: it holds the lock on
  $XDG_RUNTIME_DIR/ag-cute-music/mixer then. Watched every 2 s (Watcher); when it quits, a chosen mixer falls back to
  the system default (MainWindow.refresh_devices), as an unplugged headset.
- Chosen as the input, the call carries music: echo cancelling off and 48 kHz (SIP), stereo at a music bitrate
  (WebRTC rooms, videoroom.py). The settings are changed while it is chosen and put back when another input is
  (apply_music_mode), so Preferences show what is in effect.
- WebRTC rooms play and record the sound server's devices themselves (pulsesrc, pulsesink: SOURCE, SINK).
- Who is in the call, for the mixer's BLINK strip: the calls and rooms in progress written to CALLS (CallPublisher),
  private to the user, removed when Blink quits.
"""

import fcntl
import json
import os

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from blink.logging import ActivityLog
from blink.resources import ApplicationData


__all__ = ['DEVICE', 'DEVICE_HINT', 'installed', 'unavailable_reason', 'unavailable_hint', 'SOURCE', 'SINK', 'running', 'visible', 'selected', 'apply_music_mode', 'Watcher', 'CallPublisher']


DEVICE = 'AG Podcast Mixer'           # the ALSA device's description: its name in the device lists
SOURCE = 'cutemusic_mixer_out'          # what is sent (the sound server's names)
SINK = 'cutemusic_mixer_in'             # what is played
RUNNING = os.path.join(os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/ag-cute-music-{os.getuid()}', 'ag-cute-music', 'mixer')
MUSIC_SAMPLE_RATE = 48000
CALLS = os.path.join(os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/blink-{os.getuid()}', 'blink', 'calls.json')


DEVICE_HINT = 'AG CuteMusic\'s mixer: your microphone, music and jingles go out in the call, the others come back on their own fader'


def installed():
    """Whether AG CuteMusic is installed (its program on the path, or its ALSA device configured)."""
    import shutil
    return bool(shutil.which('ag-cute-music')) or os.path.exists('/etc/alsa/conf.d/60-ag-cute-music.conf') or \
        os.path.exists(os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'alsa', 'ag-cute-music.conf'))


def unavailable_reason():
    """Why the mixer cannot be chosen now, a few words for its menu item."""
    import platform
    if platform.system() != 'Linux':
        return 'Linux only'
    if not installed():
        return 'sudo apt install ag-cute-music'       # shown in the menu: a grayed item's tool tip may not be
    if not running():
        return 'start AG CuteMusic'
    return 'restart Blink'      # AG CuteMusic runs, but this Blink started before its device was set up


def unavailable_hint():
    """The menu item's tool tip: what to do."""
    import platform
    if platform.system() != 'Linux':
        return 'AG CuteMusic\'s mixer works as a sound device on Linux'
    if not installed():
        return 'Install AG CuteMusic to use its mixer in calls:  sudo apt install ag-cute-music'
    if not running():
        return 'Start AG CuteMusic to use its mixer in calls'
    return 'AG CuteMusic set up its sound device after Blink started: quit and start Blink again'


def running():
    """Whether AG CuteMusic runs with its mixer as a device (it holds the lock on RUNNING)."""
    try:
        with open(RUNNING) as file:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def visible(devices):
    """The devices to list: the mixer only while AG CuteMusic runs."""
    devices = list(devices)
    return devices if DEVICE not in devices or running() else [device for device in devices if device != DEVICE]


def selected(settings=None):
    """Whether the mixer is the input (the call is music)."""
    if settings is None:
        from sipsimple.configuration.settings import SIPSimpleSettings
        settings = SIPSimpleSettings()
    return settings.audio.input_device == DEVICE


def _state_path():
    return ApplicationData.get('cutemusic.json')


def apply_music_mode():
    """The mixer chosen as the input: echo cancelling off and 48 kHz, the settings before kept; another input: they
    are put back. Called when the input device changes."""
    from sipsimple.configuration.settings import SIPSimpleSettings
    settings = SIPSimpleSettings()
    path = _state_path()
    try:
        with open(path) as file:
            before = json.load(file)
    except (OSError, ValueError):
        before = None
    if selected(settings) and before is None:
        before = {'echo_canceller': settings.audio.echo_canceller.enabled, 'sample_rate': int(settings.audio.sample_rate),
                  'stereo': bool(getattr(settings.audio, 'stereo', False))}
        with open(path, 'w') as file:
            json.dump(before, file)
        settings.audio.echo_canceller.enabled = False
        settings.audio.sample_rate = MUSIC_SAMPLE_RATE
        if hasattr(settings.audio, 'stereo'):       # an SDK with stereo audio (audio.stereo)
            settings.audio.stereo = True
        settings.save()
        ActivityLog().info(f'[audio] {DEVICE}: music mode, echo cancelling off, {MUSIC_SAMPLE_RATE} Hz'
                           f'{", stereo" if getattr(settings.audio, "stereo", False) else ""} '
                           f'(were {"on" if before["echo_canceller"] else "off"}, {before["sample_rate"]} Hz)')
    elif not selected(settings) and before is not None:
        settings.audio.echo_canceller.enabled = bool(before.get('echo_canceller', True))
        settings.audio.sample_rate = int(before.get('sample_rate') or settings.audio.sample_rate)
        if hasattr(settings.audio, 'stereo'):
            settings.audio.stereo = bool(before.get('stereo', False))
        settings.save()
        try:
            os.unlink(path)
        except OSError:
            pass
        ActivityLog().info(f'[audio] {DEVICE} no longer the input: echo cancelling '
                           f'{"on" if settings.audio.echo_canceller.enabled else "off"}, {settings.audio.sample_rate} Hz again')


class Watcher(QObject):
    """changed(running): AG CuteMusic started or quit."""

    changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = running()
        self._timer = QTimer(self, interval=2000)
        self._timer.timeout.connect(self._check)
        self._timer.start()

    def _check(self):
        now = running()
        if now != self.running:
            self.running = now
            ActivityLog().info(f'[audio] AG CuteMusic {"runs: " + DEVICE + " listed" if now else "is gone: " + DEVICE + " no longer listed"}')
            self.changed.emit(now)


# Who is in the call

class CallPublisher(QObject):
    """The calls and rooms in progress, in CALLS for AG CuteMusic's mixer (its BLINK strip shows who is there):
        {"calls": [{"kind": "call", "name": ..., "address": ..., "held": <put on hold here>},
                   {"kind": "room", "name": <room>, "participants": [<name>, ...]}]}
    Read every second from Blink's own lists (the sessions with audio that are connected, the rooms joined), not
    kept from notifications: nothing is left behind when one is missed. The calls not on hold first (switching
    calls puts the other on hold: the one talked to comes first). Written when it changes; removed when Blink quits
    (stop)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._written = None
        self._timer = QTimer(self, interval=1000)
        self._timer.timeout.connect(self._update)
        self._timer.start()
        self._update()

    @staticmethod
    def _calls():
        from blink.sessions import SessionManager
        calls = []
        for session in list(SessionManager().sessions):
            try:
                if not str(session.state or '').startswith('connected') or 'audio' not in session.streams:
                    continue
                uri = session.contact_uri.uri if session.contact_uri is not None else ''
                address = str(uri).replace('sip:', '', 1)
                name = session.contact.name if session.contact is not None and session.contact.name else address
                calls.append({'kind': 'call', 'name': name, 'address': address, 'held': bool(session.local_hold)})
            except Exception:
                continue
        calls.sort(key=lambda call: call['held'])         # (stable: in Blink's order otherwise)
        return calls

    @staticmethod
    def _rooms():
        try:
            from blink.webrtc.videoroom import VideoroomManager
            sessions = list(VideoroomManager().sessions)
        except Exception:
            return []
        rooms = []
        for session in sessions:
            if getattr(session, 'state', None) not in ('accepted', 'established'):
                continue
            participants = [participant.display_name or participant.uri for participant in list(session.participants.values())
                            if not getattr(participant, 'parent', None)]
            rooms.append({'kind': 'room', 'name': session.room, 'participants': [name for name in participants if name]})
        return rooms

    def _update(self):
        calls = self._calls() + self._rooms()
        if calls == self._written:
            return
        self._written = calls
        summary = ', '.join(call['name'] + (' (on hold)' if call.get('held') else '') for call in calls) or 'none'
        ActivityLog().info(f'[audio] Calls shown in AG CuteMusic\'s mixer: {summary}')
        try:
            os.makedirs(os.path.dirname(CALLS), mode=0o700, exist_ok=True)
            temporary = CALLS + '.new'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as file:
                json.dump({'calls': calls}, file)
            os.replace(temporary, CALLS)
        except OSError as e:
            ActivityLog().warning(f'[audio] Cannot write {CALLS}: {e}')

    def stop(self):
        self._timer.stop()
        try:
            os.unlink(CALLS)
        except OSError:
            pass
