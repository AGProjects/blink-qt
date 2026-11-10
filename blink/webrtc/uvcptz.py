"""Pan, tilt and zoom of a USB camera over V4L2 (UVC camera controls), on Linux: the same interface
as blink.webrtc.ptz, so a tile drives both alike.

A camera is found by its name (as the SDK and GStreamer give it) among /sys/class/video4linux,
and asked which of the controls it has (VIDIOC_QUERYCTRL), with their ranges: pan_absolute,
tilt_absolute, zoom_absolute (a Logitech PTZ Pro 2 has these), else pan_speed and tilt_speed. A
held control with absolute controls steps the position every MOVE_INTERVAL until let go (the
camera follows at its own speed); with speed controls it sets the speed, and 0 at the end. UVC
has no presets: they are positions (pan, tilt, zoom) kept by Blink in ui.conf, per camera name.
"""

import errno
import fcntl
import glob
import json
import os
import struct
import sys
import time
import uuid

from PyQt6.QtCore import QTimer

from blink.logging import ActivityLog


__all__ = ['UvcPtz', 'find']


# linux/videodev2.h
_CAMERA_CLASS_BASE = 0x009a0900
PAN_ABSOLUTE = _CAMERA_CLASS_BASE + 8
TILT_ABSOLUTE = _CAMERA_CLASS_BASE + 9
ZOOM_ABSOLUTE = _CAMERA_CLASS_BASE + 13
PAN_SPEED = _CAMERA_CLASS_BASE + 32
TILT_SPEED = _CAMERA_CLASS_BASE + 33
PAN_RESET = _CAMERA_CLASS_BASE + 6          # buttons: back to the middle
TILT_RESET = _CAMERA_CLASS_BASE + 7
REVERSED_BY_DEFAULT = ()                    # cameras known to pan the other way: none for sure (Reverse pan in the PTZ menu)
_QUERYCTRL = struct.Struct('II32siiiiI2I')         # struct v4l2_queryctrl
_CONTROL = struct.Struct('Ii')                     # struct v4l2_control
VIDIOC_QUERYCTRL = 0xc0000000 | (_QUERYCTRL.size << 16) | (ord('V') << 8) | 36
VIDIOC_G_CTRL = 0xc0000000 | (_CONTROL.size << 16) | (ord('V') << 8) | 27
VIDIOC_S_CTRL = 0xc0000000 | (_CONTROL.size << 16) | (ord('V') << 8) | 28
CTRL_FLAG_DISABLED = 0x0001

ARC_SECONDS = 3600      # pan_absolute and tilt_absolute are in arc seconds (V4L2)
FIELD_OF_VIEW = (82.0, 52.0)    # degrees, horizontal and vertical, zoomed out: a 90 degree diagonal at 16:9 (Logitech PTZ Pro 2)
SPEED_DEGREES = 30.0    # degrees a second a camera turns at with pan_speed / tilt_speed at their maximum (a guess: UVC does not tell)
MOVE_INTERVAL = 100     # ms between the steps of a held control
STEP_FRACTION = 0.02    # of a control's range per step at full speed (1.0): 20 % a second
PRESETS_KEY = 'videoroom/uvc_ptz_presets'


def _open(path):
    return os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)


def _query(fd, control):
    """(minimum, maximum, step, default) of a control, or None when the camera has not it."""
    buffer = bytearray(_QUERYCTRL.pack(control, 0, b'', 0, 0, 0, 0, 0, 0, 0))
    try:
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buffer)
    except OSError:
        return None
    id, type, name, minimum, maximum, step, default, flags, _, _ = _QUERYCTRL.unpack(buffer)
    if flags & CTRL_FLAG_DISABLED or maximum <= minimum:
        return None
    return minimum, maximum, max(1, step), default


def _has_button(fd, control):
    """Whether the camera has this button control (a reset: no range)."""
    buffer = bytearray(_QUERYCTRL.pack(control, 0, b'', 0, 0, 0, 0, 0, 0, 0))
    try:
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buffer)
    except OSError:
        return False
    return not _QUERYCTRL.unpack(buffer)[7] & CTRL_FLAG_DISABLED


def _get(fd, control):
    buffer = bytearray(_CONTROL.pack(control, 0))
    fcntl.ioctl(fd, VIDIOC_G_CTRL, buffer)
    return _CONTROL.unpack(buffer)[1]


def _set(fd, control, value):
    fcntl.ioctl(fd, VIDIOC_S_CTRL, bytearray(_CONTROL.pack(control, int(value))))


def _same_camera(name, card):
    """Whether the SDK's or GStreamer's name of a camera is this V4L2 card (the kernel cuts it at 31 characters)."""
    name, card = name.strip().lower(), card.strip().lower()
    return bool(name and card) and (name == card or name.startswith(card) or card.startswith(name))


def find(name):
    """A UvcPtz for the camera with this name when it has pan, tilt or zoom controls, else None (blocks a moment)."""
    if not name or not sys.platform.startswith('linux'):
        return None
    for node in sorted(glob.glob('/sys/class/video4linux/video*'), key=lambda node: int(node.rsplit('video', 1)[1] or 0)):
        try:
            with open(os.path.join(node, 'name')) as file:
                card = file.read().strip()
        except OSError:
            continue
        if not _same_camera(name, card):
            continue
        path = '/dev/' + os.path.basename(node)
        try:
            fd = _open(path)
        except OSError as e:
            if e.errno not in (errno.ENOENT, errno.ENODEV):
                ActivityLog().info(f'[ptz] {card}: cannot open {path} ({e.strerror})')
            continue
        try:
            controls = {control: _query(fd, control) for control in (PAN_ABSOLUTE, TILT_ABSOLUTE, ZOOM_ABSOLUTE, PAN_SPEED, TILT_SPEED)}
            resets = [control for control in (PAN_RESET, TILT_RESET) if _has_button(fd, control)]
        finally:
            os.close(fd)
        controls = {control: limits for control, limits in controls.items() if limits is not None}
        if controls:        # the metadata node of the same camera has none: the next one is tried
            ptz = UvcPtz(path, card, controls)
            ptz.resets = resets
            ptz.refresh_status()
            ActivityLog().info(f'[ptz] {card} ({path}): PTZ over V4L2, ' +
                               ', '.join(f'{_NAMES[control]} {limits[0]}..{limits[1]}' for control, limits in controls.items()) +
                               (', driven' if ptz.can_drive() else ', by steps') +
                               (', ' + ', '.join(_NAMES[control] for control in resets) if resets else ', no reset (the middle is reckoned)'))
            return ptz
    return None


REVERSED_KEY = 'ptz/reversed_pan'


def pan_reversed(key, default=False):
    """Whether this camera (its name or host) turns the other way than it says: chosen per camera, in ui.conf."""
    import json
    from blink.resources import ui_settings
    try:
        chosen = json.loads(ui_settings().value(REVERSED_KEY, '{}') or '{}')
    except (TypeError, ValueError):
        chosen = {}
    value = chosen.get(key) if isinstance(chosen, dict) else None
    return default if value is None else bool(value)


def set_pan_reversed(key, reversed_):
    import json
    from blink.resources import ui_settings
    try:
        chosen = json.loads(ui_settings().value(REVERSED_KEY, '{}') or '{}')
    except (TypeError, ValueError):
        chosen = {}
    if not isinstance(chosen, dict):
        chosen = {}
    chosen[key] = bool(reversed_)
    ui_settings().setValue(REVERSED_KEY, json.dumps(chosen))


_AXES = {'pan': PAN_ABSOLUTE, 'tilt': TILT_ABSOLUTE, 'zoom': ZOOM_ABSOLUTE}
_NAMES = {PAN_ABSOLUTE: 'pan', TILT_ABSOLUTE: 'tilt', ZOOM_ABSOLUTE: 'zoom', PAN_SPEED: 'pan speed', TILT_SPEED: 'tilt speed', PAN_RESET: 'pan reset', TILT_RESET: 'tilt reset'}
TRAVEL = 6.0            # s at full speed from the middle to an end, at most: what is reckoned is never more


def _saved():
    from blink.resources import ui_settings
    try:
        saved = json.loads(ui_settings().value(PRESETS_KEY, '{}') or '{}')
    except (TypeError, ValueError):
        saved = {}
    return saved if isinstance(saved, dict) else {}


def _save(saved):
    from blink.resources import ui_settings
    ui_settings().setValue(PRESETS_KEY, json.dumps(saved))


class UvcPtz(object):
    def __init__(self, path, card, controls):
        self.path = path
        self.host = card                # as blink.webrtc.ptz.Ptz names its camera in the log
        self.controls = controls        # {control: (minimum, maximum, step, default)}
        self.detected_zoom = ZOOM_ABSOLUTE in controls
        self.active_preset = None       # the preset the camera was sent to last, until it is moved
        self.position = {}              # {absolute control: value}, those read
        self.listeners = []             # called when the position changes
        self._fd = None                 # open while a control is held
        self._velocity = None           # (pan, tilt, zoom) of the control held, absolute controls
        self._speeds = False            # the control held set pan_speed or tilt_speed
        self._bursts = {}               # {speed control: the number of its latest burst}
        self._drive = {}                # {speed control: the speed drive() set}
        self._speed_now = {}            # {speed control: (the speed set, when)}: to reckon where it is
        self._reckoned = {}             # {speed control: seconds at full speed away from the middle (> 0: the positive way)}
        self._homing = 0                # the number of the latest reckoned way home
        self._timer = QTimer()
        self._timer.setInterval(MOVE_INTERVAL)
        self._timer.timeout.connect(self._step)

    def __repr__(self):
        return f'<UvcPtz {self.host} {self.path} zoom={self.detected_zoom} presets={len(self.presets)}>'

    def zoom_for(self, camera):
        setting = camera.get('ptz_zoom')
        return self.detected_zoom if setting is None else bool(setting)

    @property
    def presets(self):
        """[(token, name)] of the positions saved for this camera."""
        return [(preset['token'], preset.get('name') or '') for preset in self._presets()]

    def _presets(self):
        presets = _saved().get(self.host, [])
        return [preset for preset in presets if isinstance(preset, dict) and preset.get('token')]

    def _store(self, presets):
        saved = _saved()
        if presets:
            saved[self.host] = presets
        else:
            saved.pop(self.host, None)
        _save(saved)

    # Pan the other way: the camera's right is not ours (chosen in the PTZ menu); applied in move, drive, nudge and at_limit

    @property
    def pan_reversed(self):
        return pan_reversed(self.host, default=any(name in self.host.lower() for name in REVERSED_BY_DEFAULT))

    @pan_reversed.setter
    def pan_reversed(self, value):
        set_pan_reversed(self.host, value)

    def _pan(self, value):
        return -value if self.pan_reversed else value

    # Home: the middle, when nobody is in sight (blink.webrtc.tracker)

    def go_home(self):
        """Back to the middle: the reset buttons (pan_reset, tilt_reset), else the default absolute position."""
        if self._velocity or self._speeds:
            return
        self.drive(0.0, 0.0)
        resets = list(getattr(self, 'resets', []))
        defaults = {control: self.controls[control][3] for control in (PAN_ABSOLUTE, TILT_ABSOLUTE) if control in self.controls}

        def home(fd):
            for control in resets:
                _set(fd, control, 1)
            for control, value in defaults.items():
                if (control == PAN_ABSOLUTE and PAN_RESET in resets) or (control == TILT_ABSOLUTE and TILT_RESET in resets):
                    continue
                _set(fd, control, value)
        if (resets or defaults) and self._with_device(home, 'home'):
            self.active_preset = None
            self._reckoned = {}
            QTimer.singleShot(1500, self.refresh_status)
        reckoned = [speed for absolute, reset, speed in ((PAN_ABSOLUTE, PAN_RESET, PAN_SPEED), (TILT_ABSOLUTE, TILT_RESET, TILT_SPEED))
                    if speed in self.controls and absolute not in self.controls and reset not in resets]
        if reckoned:
            self._go_home_reckoned(reckoned)

    def _go_home_reckoned(self, controls):
        """No reset and no position: back by as long, at full speed, as it was reckoned to have turned away."""
        self._homing += 1
        homing = self._homing
        self._bursts = {control: number + 1 for control, number in self._bursts.items()}      # no burst stops this
        for control in controls:
            away = self._reckoned.get(control, 0.0)
            if abs(away) < 0.05:
                continue
            minimum, maximum, step, default = self.controls[control]
            speed = minimum if away > 0 else maximum

            def stop(control=control):
                if self._homing == homing and not self._speeds:
                    self._with_device(lambda fd: self._set_speed(fd, control, 0), 'home')
                    self._drive[control] = 0
                    self._reckoned[control] = 0.0
            if self._with_device(lambda fd: self._set_speed(fd, control, speed), 'home'):
                self._drive[control] = speed        # drive() sees it turning: it stops it, or turns it its way
                self.active_preset = None
                QTimer.singleShot(int(abs(away) * 1000), stop)

    def at_limit(self, axis, direction):
        """Whether the axis ('pan', 'tilt', 'zoom') can go no further that way (direction < 0: down, > 0: up);
        False while its position is not known (with speed controls it never is)."""
        if axis == 'pan':
            direction = self._pan(direction)
        control = _AXES[axis]
        if control not in self.controls or self.position.get(control) is None:
            return False
        minimum, maximum, step, default = self.controls[control]
        return self.position[control] <= minimum if direction < 0 else self.position[control] >= maximum

    def _position_now(self, control, value):
        if value != self.position.get(control):
            self.position[control] = value
            for listener in list(self.listeners):
                listener()

    def refresh_status(self):
        """Read where the camera is: position, then the listeners."""
        controls = [control for control in _AXES.values() if control in self.controls]
        if not controls:
            return
        position = {}

        def read(fd):
            for control in controls:
                position[control] = _get(fd, control)
        if self._with_device(read, 'position'):
            for control, value in position.items():
                self._position_now(control, value)

    # Turning by degrees (blink.webrtc.tracker)

    def can_turn(self):
        return any(control in self.controls for control in (PAN_ABSOLUTE, TILT_ABSOLUTE, PAN_SPEED, TILT_SPEED))

    def field_of_view(self):
        """(horizontal, vertical) degrees seen now: FIELD_OF_VIEW, narrower as it zooms (zoom_absolute / its minimum)."""
        horizontal, vertical = FIELD_OF_VIEW
        zoom = self.position.get(ZOOM_ABSOLUTE)
        if zoom is not None and ZOOM_ABSOLUTE in self.controls:
            minimum = self.controls[ZOOM_ABSOLUTE][0]
            factor = zoom / minimum if minimum > 0 else 1.0
            if factor > 1.0:
                horizontal, vertical = horizontal / factor, vertical / factor
        return horizontal, vertical

    def nudge_degrees(self, pan=0.0, tilt=0.0):
        """Turn by this many degrees (pan > 0: right, tilt > 0: up), within the limits. Without an absolute
        control for an axis, its speed for as long as that takes at SPEED_DEGREES (a burst)."""
        if self._velocity or self._speeds:
            return          # a control is held: the user drives
        pan = self._pan(pan)
        for degrees, absolute, speed in ((pan, PAN_ABSOLUTE, PAN_SPEED), (tilt, TILT_ABSOLUTE, TILT_SPEED)):
            if degrees and absolute not in self.controls and speed in self.controls:
                self._burst(speed, degrees)
        if not self.position:
            self.refresh_status()
        targets = {}
        for control, degrees in ((PAN_ABSOLUTE, pan), (TILT_ABSOLUTE, tilt)):
            if degrees and control in self.controls and self.position.get(control) is not None:
                minimum, maximum, step, default = self.controls[control]
                value = self.position[control] + round(degrees * ARC_SECONDS / step) * step
                targets[control] = min(maximum, max(minimum, value))
        targets = {control: value for control, value in targets.items() if value != self.position.get(control)}
        if not targets:
            return

        def turn(fd):
            for control, value in targets.items():
                _set(fd, control, value)
        if self._with_device(turn, 'turn'):
            self.active_preset = None
            for control, value in targets.items():
                self._position_now(control, value)

    # Driving (blink.webrtc.tracker): with pan_speed / tilt_speed, the speed changed as often as the person moves

    DRIVE_STEPS = 4         # speeds from stop to full, at least, for it to be driven: with fewer (stop or full) it overshoots, steps instead

    def can_drive(self):
        if PAN_SPEED not in self.controls:
            return False
        minimum, maximum, step, default = self.controls[PAN_SPEED]
        return maximum / step >= self.DRIVE_STEPS

    def drive(self, pan=0.0, tilt=0.0):
        """Turn at this velocity (each -1..1 of the speed range, pan > 0: right, tilt > 0: up) until it changes;
        0, 0: stop. Set only when it changed; a control held by hand wins."""
        if self._velocity or self._speeds:
            return
        pan = self._pan(pan)
        if pan or tilt:
            self._homing += 1       # driven again: a way home under way does not stop it later
        targets = {}
        for control, value in ((PAN_SPEED, pan), (TILT_SPEED, tilt)):
            if control in self.controls:
                minimum, maximum, step, default = self.controls[control]
                limit = maximum if value > 0 else -minimum
                speed = round(abs(value) * limit / step) * step if value else 0
                if value and not speed:
                    speed = step        # the slowest it goes rather than not at all
                targets[control] = int(speed if value > 0 else -speed)
        targets = {control: speed for control, speed in targets.items() if speed != self._drive.get(control, 0)}
        if not targets:
            return

        def turn(fd):
            for control, speed in targets.items():
                self._set_speed(fd, control, speed)
        if self._with_device(turn, 'turn'):
            self.active_preset = None
            self._drive.update(targets)

    def _track(self, control, speed):
        """A speed was set: what the one before turned is added to how far from the middle it is reckoned to be."""
        now = time.monotonic()
        previous, since = self._speed_now.get(control, (0, now))
        if previous:
            minimum, maximum, step, default = self.controls[control]
            limit = maximum if previous > 0 else -minimum
            moved = (now - since) * previous / (limit or 1)
            self._reckoned[control] = max(-TRAVEL, min(TRAVEL, self._reckoned.get(control, 0.0) + moved))
        self._speed_now[control] = (speed, now)

    def _set_speed(self, fd, control, speed):
        _set(fd, control, speed)
        self._track(control, speed)

    def _burst(self, control, degrees):
        """Turn at full speed for as long as the degrees take, then stop; a newer burst of the same control takes over."""
        minimum, maximum, step, default = self.controls[control]
        speed = maximum if degrees > 0 else minimum
        if not speed:
            return
        generation = self._bursts.get(control, 0) + 1
        self._bursts[control] = generation

        def stop():
            if self._bursts.get(control) == generation and not self._speeds:
                self._with_device(lambda fd: self._set_speed(fd, control, 0), 'turn')
        if self._with_device(lambda fd: self._set_speed(fd, control, speed), 'turn'):
            self.active_preset = None
            QTimer.singleShot(max(20, int(abs(degrees) / SPEED_DEGREES * 1000)), stop)

    # Moving

    def _with_device(self, action, what):
        """action(fd), with the device open (kept open while a control is held); False when it failed."""
        try:
            fd = self._fd if self._fd is not None else _open(self.path)
        except OSError as e:
            ActivityLog().warning(f'[ptz] {self.host}: {what} failed: cannot open {self.path} ({e.strerror})')
            return False
        try:
            action(fd)
            return True
        except OSError as e:
            ActivityLog().warning(f'[ptz] {self.host}: {what} failed: {e.strerror}')
            return False
        finally:
            if fd is not self._fd:
                os.close(fd)

    def move(self, pan=0.0, tilt=0.0, zoom=0.0):
        """Move (each -1..1 of the camera's speed) until stop()."""
        self.stop()
        pan = self._pan(pan)
        self._bursts = {control: number + 1 for control, number in self._bursts.items()}     # the user drives: no burst stops this
        self._homing += 1
        self._drive = {}
        self.active_preset = None
        try:
            self._fd = _open(self.path)
        except OSError as e:
            ActivityLog().warning(f'[ptz] {self.host}: move failed: cannot open {self.path} ({e.strerror})')
            return
        absolute = [(PAN_ABSOLUTE, pan), (TILT_ABSOLUTE, tilt), (ZOOM_ABSOLUTE, zoom)]
        try:
            for value, position, speed in ((pan, PAN_ABSOLUTE, PAN_SPEED), (tilt, TILT_ABSOLUTE, TILT_SPEED)):
                if value and position not in self.controls and speed in self.controls:     # no absolute control for it: its speed
                    minimum, maximum, step, default = self.controls[speed]
                    self._set_speed(self._fd, speed, maximum if value > 0 else minimum)
                    self._speeds = True
        except OSError as e:
            ActivityLog().warning(f'[ptz] {self.host}: move failed: {e.strerror}')
        self._velocity = tuple((control, value) for control, value in absolute if value and control in self.controls) or None
        if self._velocity:
            self._step()
            self._timer.start()
        elif not self._speeds:
            self._close()

    def _step(self):
        if not self._velocity or self._fd is None:
            return
        try:
            for control, value in self._velocity:
                minimum, maximum, step, default = self.controls[control]
                delta = max(step, round((maximum - minimum) * STEP_FRACTION * abs(value) / step) * step)
                current = _get(self._fd, control)
                target = min(maximum, max(minimum, current + (delta if value > 0 else -delta)))
                if target != current:
                    _set(self._fd, control, target)
                self._position_now(control, target)
        except OSError as e:
            ActivityLog().warning(f'[ptz] {self.host}: move failed: {e.strerror}')
            self._timer.stop()
            self._velocity = None

    def stop(self):
        self._timer.stop()
        self._velocity = None
        if self._speeds and self._fd is not None:
            try:
                for control in (PAN_SPEED, TILT_SPEED):
                    if control in self.controls:
                        self._set_speed(self._fd, control, 0)
            except OSError as e:
                ActivityLog().warning(f'[ptz] {self.host}: stop failed: {e.strerror}')
        self._speeds = False
        self._close()

    def _close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    # Presets: positions kept by Blink

    def goto_preset(self, token, done=None):
        preset = next((preset for preset in self._presets() if preset['token'] == token), None)
        error = None
        if preset is None:
            error = 'no such preset'
        else:
            self.stop()

            def go(fd):
                for control, key in ((PAN_ABSOLUTE, 'pan'), (TILT_ABSOLUTE, 'tilt'), (ZOOM_ABSOLUTE, 'zoom')):
                    if control in self.controls and preset.get(key) is not None:
                        minimum, maximum, step, default = self.controls[control]
                        _set(fd, control, min(maximum, max(minimum, preset[key])))
            if self._with_device(go, 'preset'):
                self.active_preset = token
                self.refresh_status()
            else:
                error = 'the camera did not take the position'
        if done is not None:
            done(None, error)

    def set_preset(self, name, token=None, done=None):
        """Save the current position as a preset: a new one, or over token; done gets its token."""
        position = {}

        def read(fd):
            for control, key in ((PAN_ABSOLUTE, 'pan'), (TILT_ABSOLUTE, 'tilt'), (ZOOM_ABSOLUTE, 'zoom')):
                if control in self.controls:
                    position[key] = _get(fd, control)
        if not self._with_device(read, 'save preset') or not position:
            if done is not None:
                done(token, 'the camera does not tell its position')
            return
        presets = self._presets()
        token = token or uuid.uuid4().hex[:8]
        preset = dict(token=token, name=name, **position)
        presets = [preset if existing['token'] == token else existing for existing in presets]
        if not any(existing['token'] == token for existing in presets):
            presets.append(preset)
        self._store(presets)
        if done is not None:
            done(token, None)

    def remove_preset(self, token, done=None):
        self._store([preset for preset in self._presets() if preset['token'] != token])
        if done is not None:
            done(None, None)

    def refresh_presets(self, done=None):
        if done is not None:
            done(self.presets, None)
