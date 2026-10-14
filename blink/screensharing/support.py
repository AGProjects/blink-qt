"""Whether this system can take part in screen sharing, as viewer (client) and as server.

Viewing a remote screen uses the built-in RFB client, so it works wherever the
_rfb extension loads.  The extension is no longer built (setup.py) and nothing
depends on libvncclient or x11vnc: screens are shared in video calls
(screen_share.py).  The VNC code stays and works again when _rfb is built by hand
and x11vnc is installed.  Sharing my screen runs an external VNC server: x11vnc,
which can only capture an X11 session (under Wayland it sees XWayland's empty
root window), or blinkvnc.exe on Windows.  The answers do not change while
Blink runs, so they are computed once.
"""

import os
import shutil
import sys

from functools import lru_cache


__all__ = ['can_view_screens', 'can_share_my_screen', 'screen_sharing_server_problem', 'log_screen_sharing_support', 'vnc_screen_sharing_offered']


@lru_cache(maxsize=None)
def screen_sharing_client_problem():
    """None if remote screens can be viewed, otherwise why not."""
    try:
        from blink.screensharing import _rfb  # noqa: F401
    except ImportError as e:
        return 'the RFB client is not built (%s)' % e
    return None


@lru_cache(maxsize=None)
def screen_sharing_server_problem():
    """None if my screen can be shared, otherwise why not."""
    if sys.platform == 'win32':
        from blink.resources import Resources
        if not os.path.isfile(os.path.join(Resources.directory, '..', 'blinkvnc.exe')):
            return 'blinkvnc.exe is missing'
        return None
    if os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland' or os.environ.get('WAYLAND_DISPLAY'):
        return 'the desktop runs on Wayland and x11vnc can only capture an X11 session'
    if not os.environ.get('DISPLAY'):
        return 'there is no X11 display'
    if shutil.which('x11vnc') is None:
        return 'x11vnc is not installed'
    return None


# Starting a VNC screen sharing session (over MSRP) is not offered for now:
# screens are shared in video calls ("My screen" in the video window's Camera
# menu, screen_share.py) until it is decided what becomes of VNC. Incoming VNC
# offers are still handled. True puts the buttons and menu items back.
VNC_SCREEN_SHARING_OFFERED = False


def vnc_screen_sharing_offered():
    return VNC_SCREEN_SHARING_OFFERED


def can_view_screens():
    return screen_sharing_client_problem() is None


def can_share_my_screen():
    return screen_sharing_server_problem() is None


def log_screen_sharing_support():
    from blink.logging import ActivityLog
    client_problem = screen_sharing_client_problem()
    server_problem = screen_sharing_server_problem()
    if client_problem is not None:
        ActivityLog().info('[screen sharing] Viewing remote screens is not available: %s' % client_problem)
    if server_problem is not None:
        ActivityLog().info('[screen sharing] Sharing my screen is not available: %s' % server_problem)
    if client_problem is None and server_problem is None:
        ActivityLog().info('[screen sharing] Viewing remote screens and sharing my screen are available')
