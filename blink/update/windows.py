# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

import os

from ctypes import c_char_p, c_wchar_p, CDLL
from ctypes.util import find_library
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implements

from blink.update import IUpdateManager


def library_locations(name):
    library_name = '%s.dll' % (name)
    additional_paths = ['.']

    library = find_library(library_name)
    if library is not None:
        yield library
    for path in additional_paths:
        yield os.path.join(path, library_name)

def load_library(name):
    for library in library_locations(name):
        try:
            return CDLL(library)
        except OSError:
            pass
        else:
            break
    else:
        raise RuntimeError('cannot find %s on this system' % name)


# load WinSparkle dll
winsparkle_dll = load_library('WinSparkle')

# function definitions
winsparkle_init = winsparkle_dll.win_sparkle_init
winsparkle_init.argtypes = []
winsparkle_init.restype = None

winsparkle_cleanup = winsparkle_dll.win_sparkle_cleanup
winsparkle_cleanup.argtypes = []
winsparkle_cleanup.restype = None

winsparkle_set_appcast_url = winsparkle_dll.win_sparkle_set_appcast_url
winsparkle_set_appcast_url.argtypes = [c_char_p]
winsparkle_set_appcast_url.restype = None

winsparkle_check_update = winsparkle_dll.win_sparkle_check_update_with_ui
winsparkle_check_update.argtypes = []
winsparkle_check_update.restype = None

winsparkle_set_app_details = winsparkle_dll.win_sparkle_set_app_details
winsparkle_set_app_details.argtypes = [c_wchar_p, c_wchar_p, c_wchar_p]
winsparkle_set_app_details.restype = None


class UpdateManager(object):

    implements(IUpdateManager)

    def initialize(self):
        """Initialize WinSparkle library, it will try to fetch updates in the background"""
        from blink import Blink, __version__
        application = Blink()
        settings = SIPSimpleSettings()
        winsparkle_set_appcast_url(settings.server.updater_url)
        winsparkle_set_app_details(application.organizationName(), application.applicationName(), __version__)
        winsparkle_init()

    def shutdown(self):
        """Shutdown WinSparkle library. Stops pending tasks and shuts down helper threads"""
        winsparkle_cleanup()

    def check_for_updates(self):
        """Interactively check for updates"""
        winsparkle_check_update()

