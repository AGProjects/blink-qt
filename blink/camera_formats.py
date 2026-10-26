"""What a camera can deliver: its picture sizes and the highest frame rate of each.

The SDK probes every capture device when it scans for them (V4L2 on Linux, with
libv4l2 converting formats such as MJPEG) and reports (width, height, fps)
tuples. The resolutions Blink sends (CAMERA_RESOLUTIONS) are offered only when
the camera delivers them. When the SDK cannot say (an older SDK, or a driver
that lists nothing), every resolution is offered as before.

Not every device the SDK lists is a camera: its test devices (colorbar, null)
and V4L2 nodes that deliver no picture (metadata nodes, codecs) are left out
of the camera lists (usable_cameras()).
"""

__all__ = ['SYNTHETIC_CAMERAS', 'resolve_device', 'camera_formats', 'describe_formats', 'resolution_support', 'is_usable_camera', 'usable_cameras', 'log_camera_formats', 'is_external_camera']

import re
import sys

from sipsimple.application import SIPApplication

from blink.logging import ActivityLog
from blink.screen_pointer import is_screen_device
from blink.screen_share import CAMERA_RESOLUTIONS


# test devices of the SDK, no cameras to choose
SYNTHETIC_CAMERAS = {'Colorbar generator', 'Colorbar-active', 'Null video device'}


def resolve_device(device):
    """The capture device behind a setting: the system default is the first
    camera the SDK lists, the one it opens for it."""
    if device != 'system_default':
        return device
    try:
        devices = SIPApplication.engine.video_devices
    except Exception:
        return None
    return next((name for name in devices if name not in (None, 'system_default') and not is_screen_device(name)), None)


def _sdk_formats(device):
    """The formats the SDK probed for a device: a list (empty when it delivers
    none), or None when the SDK cannot say."""
    try:
        formats = SIPApplication.engine.get_video_device_formats(device)
    except Exception:       # an SDK without it, or the engine not running
        return None
    if formats is None:
        return None
    return [(int(w), int(h), float(fps)) for w, h, fps in formats]


def camera_formats(device):
    """[(width, height, fps), ...] largest first, or None when unknown."""
    device = resolve_device(device)
    if device is None or is_screen_device(device):
        return None
    return _sdk_formats(device) or None


def _sdk_lists_formats():
    """Whether the SDK reports formats at all: then a camera listing none
    delivers no picture; otherwise an empty list says nothing."""
    try:
        devices = SIPApplication.engine.video_devices
    except Exception:
        return False
    return any(_sdk_formats(device) for device in devices if device not in (None, 'system_default') and device not in SYNTHETIC_CAMERAS and not is_screen_device(device))


def is_usable_camera(device, sdk_lists_formats=None):
    """A device to offer as a camera: not the system default or None, not the
    screen, not an SDK test device, and delivering a picture."""
    if device in (None, 'system_default') or device in SYNTHETIC_CAMERAS or is_screen_device(device):
        return False
    if sdk_lists_formats is None:
        sdk_lists_formats = _sdk_lists_formats()
    return not sdk_lists_formats or _sdk_formats(device) != []


def usable_cameras(devices):
    """The cameras among devices, in their order (screens and the system default left out)."""
    sdk_lists_formats = _sdk_lists_formats()
    return [device for device in devices if is_usable_camera(device, sdk_lists_formats)]


def describe_formats(formats):
    if not formats:
        return 'unknown'
    return ', '.join('%dx%d@%s' % (w, h, ('%g' % round(fps, 1))) for w, h, fps in formats)


def resolution_support(device):
    """[(size, label, bitrate, fps), ...] for CAMERA_RESOLUTIONS: fps is the
    camera's highest rate at that size, 0 when it does not deliver it, None
    when that is not known (then the resolution is offered)."""
    formats = camera_formats(device)
    result = []
    for size, label, bitrate in CAMERA_RESOLUTIONS:
        if formats is None:
            fps = None
        else:
            fps = max((rate for w, h, rate in formats if (w, h) == (size.width, size.height)), default=0)
        result.append((size, label, bitrate, fps))
    return result


def log_camera_formats(devices):
    activity = ActivityLog()
    sdk_lists_formats = _sdk_lists_formats()
    for device in devices:
        if device in (None, 'system_default') or device in SYNTHETIC_CAMERAS or is_screen_device(device):
            continue
        if not is_usable_camera(device, sdk_lists_formats):
            activity.info('Camera %s delivers no picture, not offered' % device)
            continue
        activity.info('Camera %s resolutions: %s' % (device, describe_formats(camera_formats(device))))


# Built in or plugged in

_BUILT_IN_NAMES = re.compile(r'integrated|built-?in|internal|facetime|front|laptop|notebook|ir camera', re.I)


def _v4l2_removable():
    """{V4L2 card name: True for a camera on a removable (external) USB port, False for a fixed one} (Linux sysfs)."""
    import glob
    import os
    cameras = {}
    for node in glob.glob('/sys/class/video4linux/video*'):
        try:
            with open(os.path.join(node, 'name')) as f:
                name = f.read().strip()
            path = os.path.realpath(os.path.join(node, 'device'))
        except OSError:
            continue
        removable = None
        while path.startswith('/sys/') and removable is None:
            try:
                with open(os.path.join(path, 'removable')) as f:
                    value = f.read().strip()
                removable = True if value == 'removable' else False if value == 'fixed' else None
                if removable is None:
                    break       # unknown: the port does not say
            except OSError:
                pass
            path = os.path.dirname(path)
        if removable is not None:
            cameras[name] = cameras.get(name, False) or removable
    return cameras


def is_external_camera(device):
    """Whether the camera is plugged in rather than built in: by the USB port it is on (Linux), else by its name."""
    if sys.platform.startswith('linux'):
        try:
            removable = _v4l2_removable()
        except Exception:
            removable = {}
        for name, value in removable.items():
            if name and (device == name or device.startswith(name) or name.startswith(device)):
                return value
    return not _BUILT_IN_NAMES.search(device or '')
