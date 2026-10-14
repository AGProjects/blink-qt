
# The VNC viewer needs the _rfb extension, which is no longer built (setup.py).
# The names are loaded on first use, so that importing blink.screensharing.support
# does not need it.

__all__ = ['ScreensharingWindow', 'VNCViewer', 'VNCClient', 'RFBSettings', 'ServerDefault', 'TrueColor', 'HighColor', 'LowColor']

_modules = {'VNCClient': 'vncclient', 'RFBSettings': 'vncclient', 'ServerDefault': 'vncclient', 'TrueColor': 'vncclient', 'HighColor': 'vncclient', 'LowColor': 'vncclient',
            'ScreensharingWindow': 'vncviewer', 'VNCViewer': 'vncviewer'}


def __getattr__(name):
    if name not in _modules:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    from importlib import import_module
    value = getattr(import_module(f'{__name__}.{_modules[name]}'), name)
    globals()[name] = value
    return value
