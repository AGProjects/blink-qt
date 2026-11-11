"""Pan, tilt and zoom of a USB camera over V4L2 (UVC camera controls), on Linux: the same interface
as blink.webrtc.ptz, so a tile drives both alike.

A camera is found by its name (as the SDK and GStreamer give it) among /sys/class/video4linux,
and asked which of the controls it has (VIDIOC_QUERYCTRL), with their ranges: pan_absolute,
tilt_absolute, zoom_absolute, else pan_speed and tilt_speed (a Logitech PTZ Pro 2 has only these
and zoom). A held control with absolute controls steps the position every MOVE_INTERVAL until let
go (the camera follows at its own speed); with speed controls it sets the speed, and 0 at the end.

UVC has no presets: they are positions (pan, tilt, zoom) kept by Blink in ui.conf, per camera name.
A Logitech camera with the Peripheral Control extension unit (LOGITECH_XU_PERIPHERAL_CONTROL, as
used by PTZControl on Windows) keeps its own: 8 preset memories, saved and recalled by the camera
(the remote's preset buttons are the first ones), and a home it goes to by itself; they are driven
with UVCIOC_CTRL_QUERY on selector PANTILT_MODE (3: home, 4..11: save 1..8, 12..19: go to 1..8),
the unit's id read from the camera's USB descriptors. Blink keeps only their names
(videoroom/uvc_ptz_memory_names); memories 1..REMOTE_PRESETS are always offered.
"""

import ctypes
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
MEMORY_NAMES_KEY = 'videoroom/uvc_ptz_memory_names'
LOGITECH_PERIPHERAL_XU = bytes.fromhex('212de5ff30802c4e82d9f587d00540bd')     # FFE52D21-8030-4E2C-82D9-F587D00540BD, as in the USB descriptor
XU_PANTILT_MODE = 0x02          # its selector: preset and home
XU_HOME, XU_SAVE, XU_RECALL = 3, 4, 12      # values: home; save memory n (1..8) at 3 + n; recall it at 11 + n
MEMORIES = 8
REMOTE_PRESETS = 3              # the remote's preset buttons: the first memories, always offered
UVC_SET_CUR, UVC_GET_LEN, UVC_GET_INFO = 0x01, 0x85, 0x86
WATCH_TIMES = (0.0, 0.5, 1.5, 3.0)     # s after a move asked by hand, preset or home: the position the camera reports is logged


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


def _explain(fd, control):
    """What the camera says of a control, for the log: its range, or why it is not used."""
    buffer = bytearray(_QUERYCTRL.pack(control, 0, b'', 0, 0, 0, 0, 0, 0, 0))
    try:
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buffer)
    except OSError as e:
        return f'absent ({errno.errorcode.get(e.errno, e.errno)})'
    id, type, name, minimum, maximum, step, default, flags, _, _ = _QUERYCTRL.unpack(buffer)
    text = f'{minimum}..{maximum} step {step} default {default} flags {flags:#x}'
    if flags & CTRL_FLAG_DISABLED:
        return text + ': disabled'
    if type == 4:       # V4L2_CTRL_TYPE_BUTTON
        return 'button'
    if maximum <= minimum:
        return text + ': no range'
    try:
        text += f' now {_get(fd, control)}'
    except OSError as e:
        text += f' unreadable ({errno.errorcode.get(e.errno, e.errno)})'
    return text


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


class _ExtValue(ctypes.Union):
    _pack_ = 1
    _fields_ = [('value', ctypes.c_int32), ('value64', ctypes.c_int64), ('ptr', ctypes.c_void_p)]


class _ExtControl(ctypes.Structure):             # struct v4l2_ext_control (packed)
    _pack_ = 1
    _anonymous_ = ('u',)
    _fields_ = [('id', ctypes.c_uint32), ('size', ctypes.c_uint32), ('reserved2', ctypes.c_uint32), ('u', _ExtValue)]


class _ExtControls(ctypes.Structure):            # struct v4l2_ext_controls
    _fields_ = [('which', ctypes.c_uint32), ('count', ctypes.c_uint32), ('error_idx', ctypes.c_uint32), ('request_fd', ctypes.c_int32),
                ('reserved', ctypes.c_uint32 * 1), ('controls', ctypes.POINTER(_ExtControl))]


VIDIOC_S_EXT_CTRLS = 0xc0000000 | (ctypes.sizeof(_ExtControls) << 16) | (ord('V') << 8) | 72
_CAMERA_CLASS = 0x009a0000


def _set_together(fd, values):
    """Set controls in one VIDIOC_S_EXT_CTRLS: pan_absolute and tilt_absolute are one UVC control (PanTilt
    Absolute), and uvcvideo writes both halves on every set: set one after the other, the second write sends
    the pan the camera is still at and cancels the first. Together they go in one request. One by one when
    the driver does not take extended controls."""
    values = [(control, int(value)) for control, value in values]
    if len(values) > 1:
        array = (_ExtControl * len(values))()
        for item, (control, value) in zip(array, values):
            item.id = control
            item.value = value
        request = _ExtControls(which=_CAMERA_CLASS, count=len(values), controls=array)
        try:
            fcntl.ioctl(fd, VIDIOC_S_EXT_CTRLS, request)
            return
        except OSError as e:
            if e.errno not in (errno.EINVAL, errno.ENOTTY):
                raise
    for control, value in values:
        _set(fd, control, value)


class _XuQuery(ctypes.Structure):               # struct uvc_xu_control_query
    _fields_ = [('unit', ctypes.c_uint8), ('selector', ctypes.c_uint8), ('query', ctypes.c_uint8),
                ('size', ctypes.c_uint16), ('data', ctypes.POINTER(ctypes.c_uint8))]


UVCIOC_CTRL_QUERY = 0xc0000000 | (ctypes.sizeof(_XuQuery) << 16) | (ord('u') << 8) | 0x21


def _xu_query(fd, unit, selector, query, size, data=b''):
    """A UVC request to an extension unit's control: the bytes answered (size of them)."""
    buffer = (ctypes.c_uint8 * size)(*data[:size].ljust(size, b'\0'))
    fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, _XuQuery(unit=unit, selector=selector, query=query, size=size, data=buffer))
    return bytes(buffer)


def _xu_unit(path, guid):
    """The id of the camera's extension unit with this GUID, from its USB descriptors (sysfs), or None."""
    node = os.path.join('/sys/class/video4linux', os.path.basename(path), 'device')
    try:
        interface = os.path.realpath(node)
        with open(os.path.join(os.path.dirname(interface), 'descriptors'), 'rb') as file:
            descriptors = file.read()
    except OSError:
        return None
    index = 0
    while index + 2 <= len(descriptors):
        length = descriptors[index]
        if length < 2:
            break
        # CS_INTERFACE, VC_EXTENSION_UNIT: bUnitID, guidExtensionCode
        if descriptors[index + 1] == 0x24 and length >= 20 and descriptors[index + 2] == 0x06 and descriptors[index + 4:index + 20] == guid:
            return descriptors[index + 3]
        index += length
    return None


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
            if any(controls.values()):
                ActivityLog().info(f'[ptz] {card} ({path}): V4L2 controls: ' +
                                   '; '.join(f'{_NAMES[control]} {_explain(fd, control)}' for control in (PAN_ABSOLUTE, TILT_ABSOLUTE, ZOOM_ABSOLUTE, PAN_SPEED, TILT_SPEED, PAN_RESET, TILT_RESET)))
        finally:
            os.close(fd)
        controls = {control: limits for control, limits in controls.items() if limits is not None}
        if controls:        # the metadata node of the same camera has none: the next one is tried
            ptz = UvcPtz(path, card, controls)
            ptz.resets = resets
            ptz._find_memories()
            ptz.refresh_status()
            ActivityLog().info(f'[ptz] {card} ({path}): PTZ over V4L2, ' +
                               ', '.join(f'{_NAMES[control]} {limits[0]}..{limits[1]}' for control, limits in controls.items()) +
                               (', driven' if ptz.can_drive() else ', by steps') +
                               (', ' + ', '.join(_NAMES[control] for control in resets) if resets else
                                ', home and presets by the camera (Logitech extension unit)' if ptz.memories else ', no reset (the middle is reckoned)'))
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
        self.memories = None            # (extension unit, control size) when the camera keeps presets of its own (Logitech)
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

    # The log of what is asked and what the camera then says it does

    def _readable(self):
        return [control for control in (PAN_ABSOLUTE, TILT_ABSOLUTE, ZOOM_ABSOLUTE) if control in self.controls]

    def _describe(self, position):
        return ', '.join(f'{_NAMES[control]} {value}' for control, value in position.items()) or 'nothing'

    def _reckoned_text(self):
        return ', '.join(f'{_NAMES[control]} {seconds:+.2f} s' for control, seconds in self._reckoned.items() if abs(seconds) >= 0.01) or 'at the middle'

    def _watch(self, what, after=0.0):
        """Log the position the camera reports now and while it gets there (WATCH_TIMES); with speed controls
        only, it reports none for pan and tilt: Blink's reckoning (seconds at full speed from the middle) is logged."""
        readable = self._readable()
        self._watching = getattr(self, '_watching', 0) + 1
        watching = self._watching
        readings = []

        def sample(when):
            if watching != self._watching:
                return
            position = {}

            def read(fd):
                for control in readable:
                    position[control] = _get(fd, control)
            if readable and self._with_device(read, 'position'):
                readings.append(f'{when:.1f} s: {self._describe(position)}')
            if when == WATCH_TIMES[-1]:
                untold = [_NAMES[control] for control in (PAN_SPEED, TILT_SPEED) if control in self.controls]
                ActivityLog().info(f'[ptz] {self.host}: {what}: camera reports ' + ('; '.join(readings) or 'no position') +
                                   (f'; {" and ".join(untold).replace(" speed", "")} untold by the camera, reckoned {self._reckoned_text()}' if untold else ''))
        for when in WATCH_TIMES:
            QTimer.singleShot(int((after + when) * 1000), lambda when=when: sample(when))

    def __repr__(self):
        return f'<UvcPtz {self.host} {self.path} zoom={self.detected_zoom} presets={len(self.presets)}>'

    def zoom_for(self, camera):
        setting = camera.get('ptz_zoom')
        return self.detected_zoom if setting is None else bool(setting)

    @property
    def presets(self):
        """[(token, name)] of the presets: the camera's memories when it keeps them (the remote's always, the
        others once saved from Blink), else the positions saved for this camera."""
        if self.memories:
            names = self._memory_names()
            return [(str(number), names.get(str(number), '')) for number in range(1, MEMORIES + 1)
                    if number <= REMOTE_PRESETS or str(number) in names]
        return [(preset['token'], preset.get('name') or '') for preset in self._presets()]

    # The camera's own memories (Logitech Peripheral Control extension unit)

    def _find_memories(self):
        """Whether the camera keeps presets and a home of its own: its extension unit, and the size of its control."""
        self.memories = None        # (unit, size) when it does
        unit = _xu_unit(self.path, LOGITECH_PERIPHERAL_XU)
        if unit is None:
            return
        state = {}

        def ask(fd):
            state['size'] = int.from_bytes(_xu_query(fd, unit, XU_PANTILT_MODE, UVC_GET_LEN, 2), 'little')
            state['info'] = _xu_query(fd, unit, XU_PANTILT_MODE, UVC_GET_INFO, 1)[0]
        if self._with_device(ask, 'extension unit') and state['size'] and state['info'] & 0x02:     # it takes SET_CUR
            self.memories = (unit, state['size'])
            ActivityLog().info(f'[ptz] {self.host}: Logitech peripheral extension unit {unit}, preset control {state["size"]} bytes, '
                               f'info {state["info"]:#x}: presets and home kept by the camera')
        else:
            ActivityLog().info(f'[ptz] {self.host}: Logitech peripheral extension unit {unit}, but its preset control does not take values ({state})')

    def _memory(self, value, what):
        """Tell the camera a PANTILT_MODE value (home, save or recall); whether it took it."""
        unit, size = self.memories
        ok = self._with_device(lambda fd: _xu_query(fd, unit, XU_PANTILT_MODE, UVC_SET_CUR, size, value.to_bytes(size, 'little')), what)
        ActivityLog().info(f'[ptz] {self.host}: {what}: extension unit {unit} selector {XU_PANTILT_MODE} value {value}' + ('' if ok else ': refused'))
        return ok

    @staticmethod
    def _all_memory_names():
        from blink.resources import ui_settings
        try:
            saved = json.loads(ui_settings().value(MEMORY_NAMES_KEY, '{}') or '{}')
        except (TypeError, ValueError):
            saved = {}
        return saved if isinstance(saved, dict) else {}

    def _memory_names(self):
        names = self._all_memory_names().get(self.host)
        return names if isinstance(names, dict) else {}

    def _store_memory_names(self, names):
        from blink.resources import ui_settings
        saved = self._all_memory_names()
        if names:
            saved[self.host] = names
        else:
            saved.pop(self.host, None)
        ui_settings().setValue(MEMORY_NAMES_KEY, json.dumps(saved))

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
        """Back to the middle: the camera's own home (Logitech extension unit), else the reset buttons (pan_reset,
        tilt_reset), else the default absolute position, else reckoned."""
        if self._velocity or self._speeds:
            return
        if self.memories:
            self.drive(0.0, 0.0)
            self._homing += 1
            if self._memory(XU_HOME, 'home'):
                self.active_preset = None
                self._reckoned = {}
                self._watch('home')
            return
        self.drive(0.0, 0.0)
        resets = list(getattr(self, 'resets', []))
        defaults = {control: self.controls[control][3] for control in (PAN_ABSOLUTE, TILT_ABSOLUTE) if control in self.controls}

        ActivityLog().info(f'[ptz] {self.host}: going home: ' +
                           (f'reset {", ".join(_NAMES[control] for control in resets)}; ' if resets else '') +
                           (f'set {self._describe(defaults)}; ' if defaults else '') +
                           f'reckoned before: {self._reckoned_text()}')

        def home(fd):
            for control in resets:
                _set(fd, control, 1)
            _set_together(fd, [(control, value) for control, value in defaults.items()
                                if not ((control == PAN_ABSOLUTE and PAN_RESET in resets) or (control == TILT_ABSOLUTE and TILT_RESET in resets))])
        if (resets or defaults) and self._with_device(home, 'home'):
            self.active_preset = None
            self._reckoned = {}
            QTimer.singleShot(1500, self.refresh_status)
        reckoned = [speed for absolute, reset, speed in ((PAN_ABSOLUTE, PAN_RESET, PAN_SPEED), (TILT_ABSOLUTE, TILT_RESET, TILT_SPEED))
                    if speed in self.controls and absolute not in self.controls and reset not in resets]
        if reckoned:
            self._go_home_reckoned(reckoned)
        longest = max([abs(self._reckoned.get(control, 0.0)) for control in reckoned] or [0.0])
        self._watch('home', after=longest)      # once the reckoned turn back is over

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

            ActivityLog().info(f'[ptz] {self.host}: home by reckoning: {_NAMES[control]} {speed} for {abs(away):.2f} s')

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

    DRIVE_STEPS = 4         # speeds from stop to full, at least, for it to be driven proportionally

    def can_drive(self):
        """Whether the tracker drives it (the speed decided on every look) rather than turning it by steps. A
        camera with fewer speeds (stop or full: a Logitech PTZ Pro 2) is driven at full speed too, stopped ahead
        of the target, until the tracker sees it hunt around it (stepping)."""
        if PAN_SPEED not in self.controls or getattr(self, 'stepping', False):
            return False
        return True

    @property
    def full_speed_only(self):
        """Only stop or full speed: driven, it is stopped ahead of the target (the tracker's LEAD_FULL_SPEED)."""
        if PAN_SPEED not in self.controls:
            return False
        minimum, maximum, step, default = self.controls[PAN_SPEED]
        return maximum / step < self.DRIVE_STEPS

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
        ActivityLog().info(f'[ptz] {self.host}: pad pressed: pan {pan:+.2f}, tilt {tilt:+.2f}, zoom {zoom:+.2f}' +
                           (' (pan reversed)' if self.pan_reversed and pan else ''))
        self._pressed = time.monotonic()
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
                    ActivityLog().info(f'[ptz] {self.host}: {_NAMES[speed]} {maximum if value > 0 else minimum} (no absolute position for it)')
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
        held = getattr(self, '_pressed', None)
        self._pressed = None
        if held is not None:
            sent = 'speeds' if self._speeds else 'steps' if self._velocity else 'nothing'
            ActivityLog().info(f'[ptz] {self.host}: pad released after {time.monotonic() - held:.2f} s ({sent})')
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
        if held is not None:
            self._watch('after the pad')

    def _close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    # Presets: positions kept by Blink

    def goto_preset(self, token, done=None):
        if self.memories:
            self.stop()
            ok = token.isdigit() and 1 <= int(token) <= MEMORIES and self._memory(XU_RECALL + int(token) - 1, f'go to preset {token} ({self._memory_names().get(token) or "unnamed"})')
            if ok:
                self.active_preset = token
                self._reckoned = {}
                self._watch(f'preset {token}')
            if done is not None:
                done(None, None if ok else 'the camera did not take it')
            return
        preset = next((preset for preset in self._presets() if preset['token'] == token), None)
        error = None
        if preset is None:
            error = 'no such preset'
            ActivityLog().warning(f'[ptz] {self.host}: go to preset {token}: no such preset')
        else:
            self.stop()

            def go(fd):
                before = {control: _get(fd, control) for control in (PAN_ABSOLUTE, TILT_ABSOLUTE, ZOOM_ABSOLUTE) if control in self.controls}
                values = {}
                for control, key in ((PAN_ABSOLUTE, 'pan'), (TILT_ABSOLUTE, 'tilt'), (ZOOM_ABSOLUTE, 'zoom')):
                    if control in self.controls and preset.get(key) is not None:
                        minimum, maximum, step, default = self.controls[control]
                        values[control] = min(maximum, max(minimum, preset[key]))
                pan_tilt = [(control, value) for control, value in values.items() if control in (PAN_ABSOLUTE, TILT_ABSOLUTE)]
                if pan_tilt:
                    _set_together(fd, pan_tilt)
                if ZOOM_ABSOLUTE in values:
                    _set(fd, ZOOM_ABSOLUTE, values[ZOOM_ABSOLUTE])
                after = {control: _get(fd, control) for control in before}
                describe = lambda position: ', '.join(f'{_NAMES[control]} {value}' for control, value in position.items()) or 'unknown'
                ActivityLog().info(f'[ptz] {self.host}: go to preset {preset.get("name") or token} ({token}): requested {describe(values)}; '
                                   f'was at {describe(before)}; reports {describe(after)} after the request')
            if self._with_device(go, 'preset'):
                self.active_preset = token
                self.refresh_status()
                self._watch(f'preset {preset.get("name") or token}')
            else:
                error = 'the camera did not take the position'
        if done is not None:
            done(None, error)

    def set_preset(self, name, token=None, done=None):
        """Save the current position as a preset: a new one, or over token; done gets its token."""
        if self.memories:
            names = self._memory_names()
            if token is None:       # a new one: the first memory past the remote's not named yet
                token = next((str(number) for number in range(REMOTE_PRESETS + 1, MEMORIES + 1) if str(number) not in names), None)
                if token is None:
                    if done is not None:
                        done(None, f'the camera keeps {MEMORIES} presets: all are in use (Move here replaces one)')
                    return
            if not self._memory(XU_SAVE + int(token) - 1, f'save preset {token} as {name}'):
                if done is not None:
                    done(token, 'the camera did not take it')
                return
            names[token] = name
            self._store_memory_names(names)
            if done is not None:
                done(token, None)
            return
        position = {}

        def read(fd):
            for control, key in ((PAN_ABSOLUTE, 'pan'), (TILT_ABSOLUTE, 'tilt'), (ZOOM_ABSOLUTE, 'zoom')):
                if control in self.controls:
                    position[key] = _get(fd, control)
        untold = [_NAMES[speed].replace(' speed', '') for absolute, speed in ((PAN_ABSOLUTE, PAN_SPEED), (TILT_ABSOLUTE, TILT_SPEED))
                  if speed in self.controls and absolute not in self.controls]
        if untold:
            message = f'the camera does not tell its {" and ".join(untold)} (speed controls only): a preset would hold the zoom only'
            ActivityLog().warning(f'[ptz] {self.host}: save preset: {message}')
            if done is not None:
                done(token, message)
            return
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

    def rename_preset(self, token, name):
        """The name of a preset, kept with its position."""
        if self.memories:
            names = self._memory_names()
            names[token] = name
            self._store_memory_names(names)
            return
        self._store([dict(preset, name=name) if preset['token'] == token else preset for preset in self._presets()])

    def remove_preset(self, token, done=None):
        if self.memories:       # the camera cannot forget one: Blink forgets its name, the remote's stay offered
            names = self._memory_names()
            names.pop(token, None)
            self._store_memory_names(names)
            if done is not None:
                done(None, None)
            return
        self._store([preset for preset in self._presets() if preset['token'] != token])
        if done is not None:
            done(None, None)

    def refresh_presets(self, done=None):
        if done is not None:
            done(self.presets, None)
