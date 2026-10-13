"""What a camera can deliver: its picture sizes and the highest frame rate of each.

The SDK probes every capture device when it scans for them (V4L2 on Linux, with
libv4l2 converting formats such as MJPEG) and reports (width, height, fps)
tuples. The resolutions Blink sends (CAMERA_RESOLUTIONS) are offered only when
the camera delivers them. When the SDK cannot say (an older SDK, or a driver
that lists nothing), every resolution is offered as before.
"""

__all__ = ['resolve_device', 'camera_formats', 'describe_formats', 'resolution_support', 'log_camera_formats']

from sipsimple.application import SIPApplication

from blink.logging import ActivityLog
from blink.screen_pointer import is_screen_device
from blink.screen_share import CAMERA_RESOLUTIONS


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


def camera_formats(device):
    """[(width, height, fps), ...] largest first, or None when unknown."""
    device = resolve_device(device)
    if device is None or is_screen_device(device):
        return None
    try:
        formats = SIPApplication.engine.get_video_device_formats(device)
    except Exception:       # an SDK without it, or the engine not running
        return None
    return [(int(w), int(h), float(fps)) for w, h, fps in formats] or None


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
    for device in devices:
        if device in (None, 'system_default') or is_screen_device(device):
            continue
        activity.info('Camera %s resolutions: %s' % (device, describe_formats(camera_formats(device))))
