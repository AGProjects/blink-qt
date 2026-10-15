"""VNC screen sharing (over MSRP) is obsolete: screens are shared in video calls
(screen_share.py).  Neither viewing a remote screen nor sharing mine over VNC is
offered, and incoming VNC offers are declined.  The VNC code stays for now.
"""


__all__ = ['can_view_screens', 'can_share_my_screen', 'vnc_screen_sharing_offered']


def vnc_screen_sharing_offered():
    return False


def can_view_screens():
    return False


def can_share_my_screen():
    return False
