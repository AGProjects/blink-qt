"""AG CuteMusic's mixer as a sound device: "AG Podcast Mixer" (docs/jamming-ag-cutemusic.md,
docs/podcast-mixer-link-migration.md).

The AG Podcast Mixer link (python3-sipsimple docs/PodcastMixerLink.md): AG CuteMusic and Blink exchange PCM through
two shared-memory rings in the link directory (agpm.link_dir()), which the SDK turns into an audio device of that
name. What Blink records from it is the karaoke (the singer, and the music), what Blink plays on it goes to AG
CuteMusic's mixer, heard in the singer's headset.

- The device is listed (Devices menu, Preferences) only while AG CuteMusic runs: it holds mixer.lock in the link
  directory then. Watched every 2 s (Watcher); when it starts or quits the engine's sound devices are read again
  (MainWindow.refresh_devices), so the device comes and goes without a restart; a chosen mixer that is gone falls
  back to the system default, as an unplugged headset.
- Blink holds blink.lock in the link directory while it runs (CallPublisher): AG CuteMusic shows its BLINK strip.
- Chosen as the input, the call carries music: echo cancelling off and 48 kHz (SIP), stereo at a music bitrate
  (WebRTC rooms, videoroom.py). The settings are changed while it is chosen and put back when another input is
  (apply_music_mode), so Preferences show what is in effect.
- WebRTC rooms read and write the link's rings themselves (blink/webrtc/podcast_mixer.py: an appsrc on to-call, an
  appsink on from-call), as the SDK's device does for SIP calls.
- Who is in the call, for the mixer's BLINK strip: the calls and rooms in progress written to CALLS, calls.json in the
  link directory (CallPublisher), private to the user, removed when Blink quits.
"""

import json
import os

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from blink import agpm
from blink.logging import ActivityLog
from blink.resources import ApplicationData


__all__ = ['DEVICE', 'DEVICE_HINT', 'installed', 'app_running', 'unavailable_reason', 'unavailable_hint', 'running', 'visible', 'selected', 'apply_music_mode', 'Watcher', 'CallPublisher']


DEVICE = agpm.DEVICE                    # 'AG Podcast Mixer': its name in the device lists
LINK_DIR = agpm.link_dir()               # the SDK's pjmedia_agpm_dir(): mixer.lock, blink.lock, the rings, calls.json
MUSIC_SAMPLE_RATE = 48000
CALLS = agpm.path('calls.json')


DEVICE_HINT = 'AG CuteMusic\'s mixer: your microphone, music and jingles go out in the call, the others come back on their own fader'


def installed():
    """Whether AG CuteMusic is installed (its program on the path, or its ALSA device configured)."""
    import shutil
    return bool(shutil.which('ag-cute-music')) or os.path.exists('/etc/alsa/conf.d/60-ag-cute-music.conf') or \
        os.path.exists(os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'alsa', 'ag-cute-music.conf'))


def app_running():
    """Whether AG CuteMusic runs, its mixer on or not (it holds mixer.lock only while the mixer's POWER is on)."""
    import sys
    if sys.platform.startswith('linux'):
        try:
            for pid in os.listdir('/proc'):
                if not pid.isdigit() or int(pid) == os.getpid():
                    continue
                try:
                    with open(f'/proc/{pid}/cmdline', 'rb') as file:
                        arguments = file.read().split(b'\0')
                except OSError:
                    continue
                if any(os.path.basename(argument) == b'ag-cute-music' for argument in arguments[:2]):
                    return True
            return False
        except OSError:
            return False
    import subprocess
    try:
        return subprocess.run(['pgrep', '-f', 'ag-cute-music'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def unavailable_reason():
    """Why the mixer cannot be chosen now, a few words for its menu item."""
    import platform
    if platform.system() == 'Windows':
        return 'not on Windows'
    if not installed():
        return 'sudo apt install ag-cute-music'       # shown in the menu: a grayed item's tool tip may not be
    if app_running():
        return 'turn on POWER in AG CuteMusic\'s mixer'   # it runs, its mixer is off
    return 'start AG CuteMusic and turn on POWER in its mixer'     # (running, the device is listed at the Watcher's next refresh)


def unavailable_hint():
    """The menu item's tool tip: what to do."""
    import platform
    if platform.system() == 'Windows':
        return 'AG CuteMusic\'s mixer is not a sound device on Windows yet'
    if not installed():
        return 'Install AG CuteMusic to use its mixer in calls:  sudo apt install ag-cute-music'
    if app_running():
        return 'AG CuteMusic runs, its mixer is off: turn on POWER in its Mixer view to use it in calls'
    return 'Start AG CuteMusic and turn on POWER in its Mixer view to use its mixer in calls'


def running():
    """Whether AG CuteMusic runs with its mixer as a device: it holds mixer.lock in the link directory (or the SDK
    says so: the lock and the rings are there)."""
    if agpm.mixer_running():
        return True
    try:
        from sipsimple.application import SIPApplication
        return bool(SIPApplication.engine.podcast_mixer_available)
    except Exception:       # the engine not started, or an SDK without the link
        return False


def visible(devices):
    """The devices to list: the mixer only while AG CuteMusic runs, and once (the SDK's device and the ALSA device of
    the same name, AG CuteMusic's share.py, are both there on Linux until the cleanup)."""
    devices = list(devices)
    if DEVICE not in devices:
        return devices
    shown = running()
    listed = []
    for device in devices:
        if device == DEVICE:
            if not shown:
                continue
            shown = False
        listed.append(device)
    return listed


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
    """Blink seen by AG CuteMusic: blink.lock held in the link directory while Blink runs (its BLINK strip shown), and
    the calls and rooms in progress, in CALLS for AG CuteMusic's mixer (its BLINK strip shows who is there):
        {"calls": [{"kind": "call", "name": ..., "address": ..., "held": <put on hold here>},
                   {"kind": "room", "name": <room>, "participants": [<name>, ...]}]}
    Read every second from Blink's own lists (the sessions with audio that are connected, the rooms joined), not
    kept from notifications: nothing is left behind when one is missed. The calls not on hold first (switching
    calls puts the other on hold: the one talked to comes first). Written when it changes; removed, and blink.lock
    released, when Blink quits (stop)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = agpm.ProcessLock('blink.lock')
        try:
            if not self._lock.acquire():        # another Blink (another profile) holds it: AG CuteMusic sees one
                ActivityLog().info(f'[audio] Another Blink holds {agpm.path("blink.lock")}')
        except OSError as e:
            ActivityLog().warning(f'[audio] Cannot hold {agpm.path("blink.lock")}: {e}')
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
        self._lock.release()
