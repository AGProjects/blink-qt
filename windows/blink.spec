# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Blink on Windows (MSYS2 UCRT64). Run through windows/build from the
# repository root. The result is dist/blink/blink.exe with everything else under dist/blink/lib.

import os

from PyInstaller.utils.hooks import collect_submodules

from sipsimple.payloads import XMLDocument


root = os.path.abspath(os.path.join(SPECPATH, '..'))
distribution = os.path.join(SPECPATH, 'distribution')

# blink imports some of its modules lazily (in functions, or only when a feature is used)
hidden_imports = collect_submodules('blink') + collect_submodules('application.version') + [
    'PyQt6.QtMultimedia',
    'PyQt6.QtNetwork',
    'PyQt6.QtPdf',
    'PyQt6.QtPositioning',
    'PyQt6.QtSvg',
    'PyQt6.QtSvgWidgets',
    'gi.repository.Gst',
    'gi.repository.Gio',
    'gi.repository.GLib',
]

# what Windows builds go without: no QtWebEngine (not in MSYS2), so no server tools and no
# Google contacts (both need a web view); the macOS-only modules
excludes = [
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtWebChannel',
    'PyQt6.QtDBus',
    'blink.servertools',
    'blink.macos_notification',
    'googleapiclient',
    'google_auth_oauthlib',
    'google.auth',
    'google.oauth2',
    'objc',
    'AppKit',
    'Foundation',
    'tkinter',
]
hidden_imports = [name for name in hidden_imports if not any(name == excluded or name.startswith(excluded + '.') for excluded in excludes)]

datas = [
    (XMLDocument.schema_path, os.path.join('resources', 'xml-schemas')),
    (os.path.join(root, 'resources'), 'resources'),
]

binaries = [
    (os.path.join(distribution, 'WinSparkle.dll'), '.'),     # the updater, x64 build
]

a = Analysis(
    [os.path.join(root, 'bin', 'blink')],
    pathex=[root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},     # all of GStreamer's plugins: voice notes (AAC in .m4a) and media playback
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='blink',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(root, 'resources', 'icons', 'blink.ico'),
    contents_directory='lib',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='blink',
)
