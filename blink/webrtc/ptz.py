"""Pan, tilt and zoom of an RTSP camera over ONVIF (PTZ service), on the local network; ported from AG CuteCams.

A camera is asked once, when its extra video first plays (probe): its PTZ service address
(GetCapabilities), the media profile with a PTZ configuration (GetProfiles), whether it zooms, and
its presets (GetPresets). It zooms when its PTZ configuration has a zoom space and its PTZ node
(GetNodes) supports continuous zoom: some cameras without a zoom give the first. The answer is
kept per camera (address, user, password) for the session. Moves are ContinuousMove while a
control is held, then Stop; a preset is GotoPreset; SetPreset saves the current position as a
preset (a new one, or over one given), RemovePreset deletes one. The SOAP calls
(blink.webrtc.camerafinder) block: they run one after the other in the camera's own thread, in
the order asked; done, when given, gets (the answer, or None, and the error, or None) in the GUI
thread.
"""


import queue
import threading
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

from blink.logging import ActivityLog
from blink.util import call_in_gui_thread
from blink.webrtc.camerafinder import _clock_offset, _find, _local_name, _soap


__all__ = ['Ptz', 'probe', 'preset_label', 'SPEED']


TPTZ = 'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
SPEED = 0.5             # of the camera's continuous velocity range (-1..1)
STATUS_INTERVAL = 400   # ms between the questions for the zoom position while zooming

_known = {}             # (url, user, password): Ptz, or False when the camera has no PTZ; None while asked
_waiting = {}           # the same key: [callback] waiting for the answer


def _host(url):
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _on_host(address, host):
    """An address the camera gives, on the host we reach it at (some give an internal or empty one)."""
    try:
        parts = urlsplit(address)
        netloc = (f'[{host}]' if ':' in host else host) + (f':{parts.port}' if parts.port else '')
        return urlunsplit((parts.scheme or 'http', netloc, parts.path, parts.query, ''))
    except ValueError:
        return address


def probe(camera, callback):
    """callback(Ptz or None), in the GUI thread, once it is known whether the camera has PTZ."""
    key = (camera['url'], camera.get('user') or '', camera.get('password') or '')
    known = _known.get(key, 'unknown')
    if known not in ('unknown', None):
        callback(known or None)
        return
    _waiting.setdefault(key, []).append(callback)
    if known is None:
        return      # being asked
    _known[key] = None

    def work():
        try:
            ptz = Ptz.ask(*key)
        except Exception as e:
            ActivityLog().info(f'[ptz] {_host(key[0])}: no PTZ ({e})')
            ptz = None
        call_in_gui_thread(_answered, key, ptz)
    threading.Thread(target=work, name=f'ptz probe {_host(key[0])}', daemon=True).start()


def _answered(key, ptz):
    _known[key] = ptz or False
    for callback in _waiting.pop(key, []):
        callback(ptz)


class Ptz(object):
    def __init__(self, host, user, password, url, profile, zoom, presets, offset, ranges=None):
        self.host = host
        self.user, self.password = user, password
        self.url = url                  # the PTZ service
        self.profile = profile          # the media profile token with the PTZ configuration
        self.detected_zoom = zoom       # whether the camera says it zooms
        self.presets = presets          # [(token, name)]
        self.active_preset = None       # the preset the camera was sent to last, until it is moved
        self.offset = offset            # the camera's clock minus ours (WS-Security)
        self.ranges = ranges or {}      # {'pan', 'tilt', 'zoom': (minimum, maximum)} of the position (PanTiltLimits, ZoomLimits)
        self.position = {}              # {'pan', 'tilt', 'zoom': where it is} (GetStatus), those known
        self.listeners = []             # called (in the GUI thread) when the position changes
        self._status_timer = None       # asking the position while moving
        self.queue = queue.Queue()
        threading.Thread(target=self._run, name=f'ptz {host}', daemon=True).start()

    def __repr__(self):
        return f'<Ptz {self.host} zoom={self.detected_zoom} presets={len(self.presets)}>'

    def zoom_for(self, camera):
        """Whether to show the zoom buttons: the camera's own setting (ptz_zoom: True/False), else what was detected."""
        setting = camera.get('ptz_zoom')
        return self.detected_zoom if setting is None else bool(setting)

    @classmethod
    def ask(cls, url, user, password):
        """Whether the camera has PTZ (blocks; raises RuntimeError with the reason when not)."""
        host = _host(url)
        if not host:
            raise RuntimeError('no host')
        device = f'http://{f"[{host}]" if ":" in host else host}/onvif/device_service'
        offset = _clock_offset(device) if user else 0.0
        capabilities = _soap(device, '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>', user, password, offset)
        ptz = _find(capabilities, 'PTZ')
        ptz_url = _find(ptz, 'XAddr') if ptz is not None else None
        if ptz_url is None or not (ptz_url.text or '').strip():
            raise RuntimeError('the camera has no PTZ service')
        ptz_url = _on_host(ptz_url.text.strip(), host)
        media = _find(capabilities, 'Media')
        media_url = _find(media, 'XAddr') if media is not None else None
        media_url = _on_host(media_url.text.strip(), host) if media_url is not None and media_url.text else device
        answer = _soap(media_url, '<trt:GetProfiles/>', user, password, offset)
        profiles = [element for element in answer.iter() if _local_name(element.tag) == 'Profiles' and element.get('token')]
        with_ptz = [profile for profile in profiles if _find(profile, 'PTZConfiguration') is not None]
        if not with_ptz:
            raise RuntimeError('no media profile has a PTZ configuration')
        profile = with_ptz[0]
        configuration = _find(profile, 'PTZConfiguration')
        zoom = any(_local_name(element.tag) in ('DefaultContinuousZoomVelocitySpace', 'ZoomLimits') for element in configuration.iter())
        node_token = configuration.find('{*}NodeToken')
        node_token = (node_token.text or '').strip() if node_token is not None else ''
        if zoom:
            try:
                answer = _soap(ptz_url, f'<tptz:GetNodes {TPTZ}/>', user, password, offset)
                nodes = [element for element in answer.iter() if _local_name(element.tag) == 'PTZNode']
                node = next((element for element in nodes if element.get('token') == node_token), nodes[0] if nodes else None)
                if node is not None:
                    spaces = _find(node, 'SupportedPTZSpaces')
                    zoom = spaces is not None and _find(spaces, 'ContinuousZoomVelocitySpace') is not None
            except Exception as e:
                ActivityLog().info(f'[ptz] {host}: no PTZ nodes ({e}), zoom from the configuration only')
        ranges = {'pan': (-1.0, 1.0), 'tilt': (-1.0, 1.0), 'zoom': (0.0, 1.0)}      # the generic spaces, unless told
        for limits_name, axes in (('PanTiltLimits', (('pan', 'XRange'), ('tilt', 'YRange'))), ('ZoomLimits', (('zoom', 'XRange'),))):
            limits = _find(configuration, limits_name)
            for axis, range_name in axes:
                found = _find(limits, range_name) if limits is not None else None
                try:
                    ranges[axis] = (float(_find(found, 'Min').text), float(_find(found, 'Max').text))
                except (AttributeError, TypeError, ValueError):
                    pass
        token = profile.get('token')
        try:
            presets = _presets(_soap(ptz_url, _get_presets(token), user, password, offset))
        except Exception as e:
            presets = []
            ActivityLog().info(f'[ptz] {host}: no presets ({e})')
        ptz = cls(host, user, password, ptz_url, token, zoom, presets, offset, ranges)
        try:
            ptz.position = _position(_soap(ptz_url, _get_status(token), user, password, offset))
        except Exception as e:
            ActivityLog().info(f'[ptz] {host}: no PTZ status ({e})')
        ActivityLog().info(f'[ptz] {host}: PTZ, profile {token}, zoom {"yes" if zoom else "no"}, {len(presets)} presets')
        return ptz

    def at_limit(self, axis, direction):
        """Whether the axis ('pan', 'tilt', 'zoom') can go no further that way (direction < 0: down, > 0: up);
        False while its position is not known."""
        position, limits = self.position.get(axis), self.ranges.get(axis)
        if position is None or limits is None:
            return False
        low, high = limits
        margin = (high - low) * 0.005
        return position <= low + margin if direction < 0 else position >= high - margin

    def _notify(self):
        for listener in list(self.listeners):
            listener()

    def refresh_status(self):
        """Ask the camera where it is: position, then the listeners."""
        def answered(answer, error):
            position = _position(answer) if answer is not None else {}
            if position and position != self.position:
                self.position = position
                self._notify()
        self._call('status', _get_status(self.profile), answered)

    def _poll_status(self, moving):
        """While moving, the position every STATUS_INTERVAL; stopped, once more as it settles."""
        from PyQt6.QtCore import QTimer
        if self._status_timer is None:
            self._status_timer = QTimer()
            self._status_timer.setInterval(STATUS_INTERVAL)
            self._status_timer.timeout.connect(self.refresh_status)
        if moving:
            self._status_timer.start()
        else:
            self._status_timer.stop()
            self.refresh_status()
            QTimer.singleShot(1000, self.refresh_status)

    # Asked from the GUI thread, done in the camera's thread in order

    def move(self, pan=0.0, tilt=0.0, zoom=0.0):
        """Continuous move (each -1..1 of the camera's speed range) until stop()."""
        self.active_preset = None
        velocity = ''
        if pan or tilt:
            velocity += f'<tt:PanTilt x="{pan:.2f}" y="{tilt:.2f}"/>'
        if zoom:
            velocity += f'<tt:Zoom x="{zoom:.2f}"/>'
        if velocity:
            self._poll_status(True)
            self._call('move', f'<tptz:ContinuousMove {TPTZ}><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>'
                               f'<tptz:Velocity>{velocity}</tptz:Velocity></tptz:ContinuousMove>')

    def stop(self):
        if self._status_timer is not None and self._status_timer.isActive():
            self._poll_status(False)
        self._call('stop', f'<tptz:Stop {TPTZ}><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>'
                           f'<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>')

    def goto_preset(self, token, done=None):
        from PyQt6.QtCore import QTimer
        self.active_preset = token
        self._call('preset', f'<tptz:GotoPreset {TPTZ}><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>'
                             f'<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:GotoPreset>', done)
        for delay in (1000, 3000):      # the position there, once the camera got there
            QTimer.singleShot(delay, self.refresh_status)

    def set_preset(self, name, token=None, done=None):
        """Save the current position as a preset: a new one, or over token; done gets its token."""
        existing = f'<tptz:PresetToken>{escape(token)}</tptz:PresetToken>' if token else ''
        body = (f'<tptz:SetPreset {TPTZ}><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>'
                f'<tptz:PresetName>{escape(name)}</tptz:PresetName>{existing}</tptz:SetPreset>')

        def answered(answer, error):
            if done is not None:
                element = _find(answer, 'PresetToken') if answer is not None else None
                done((element.text or '').strip() if element is not None and element.text else token, error)
        self._call('save preset', body, answered)

    def remove_preset(self, token, done=None):
        self._call('delete preset', f'<tptz:RemovePreset {TPTZ}><tptz:ProfileToken>{escape(self.profile)}</tptz:ProfileToken>'
                                    f'<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:RemovePreset>', done)

    def refresh_presets(self, done=None):
        """Ask the camera for its presets again: self.presets, then done(presets, error)."""
        def answered(answer, error):
            if answer is not None:
                self.presets = _presets(answer)
            if done is not None:
                done(self.presets, error)
        self._call('presets', _get_presets(self.profile), answered)

    def _call(self, what, body, done=None):
        self.queue.put((what, body, done))

    def _run(self):
        while True:
            what, body, done = self.queue.get()
            answer = error = None
            try:
                answer = _soap(self.url, body, self.user, self.password, self.offset)
            except Exception as e:
                error = str(e)
                ActivityLog().warning(f'[ptz] {self.host}: {what} failed: {e}')
            if done is not None:
                call_in_gui_thread(done, answer, error)


def _get_status(profile):
    return f'<tptz:GetStatus {TPTZ}><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken></tptz:GetStatus>'


def _position(answer):
    """{'pan', 'tilt', 'zoom': value} of a GetStatus answer, those it tells."""
    found = _find(answer, 'Position')
    position = {}
    for element_name, axes in (('PanTilt', (('pan', 'x'), ('tilt', 'y'))), ('Zoom', (('zoom', 'x'),))):
        element = _find(found, element_name) if found is not None else None
        for axis, attribute in axes:
            try:
                position[axis] = float(element.get(attribute))
            except (AttributeError, TypeError, ValueError):
                pass
    return position


def _get_presets(profile):
    return f'<tptz:GetPresets {TPTZ}><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken></tptz:GetPresets>'


def _presets(answer):
    """[(token, the camera's name for it, '' when none)] of a GetPresets answer."""
    presets = []
    for element in answer.iter():
        if _local_name(element.tag) == 'Preset' and element.get('token'):
            name = _find(element, 'Name')
            presets.append((element.get('token'), (name.text or '').strip() if name is not None and name.text else ''))
    return presets


def preset_label(camera, token, name):
    """How a preset is shown: our name for it (preset_names), else the camera's, else its number."""
    return (camera.get('preset_names') or {}).get(token) or name or f'Preset {token}'
