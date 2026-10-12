# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

# These packages use Cython/compiled or lazy (runtime, by-name) imports that
# PyInstaller's static analysis cannot see. The most notable is the SDK's
# Cython core doing `from application.version import version`, which otherwise
# fails at startup with: ModuleNotFoundError: No module named 'application.version'.
# Collect every submodule so nothing gets dropped.
def _submodules(package):
    try:
        return collect_submodules(package)
    except Exception:
        return []

hiddenimports = [
    'application',
    # Qt bindings reached only through .ui promoted widgets, which uic.loadUiType
    # imports at runtime — static analysis misses these, e.g.
    # blink/widgets/spinner.py does `from PyQt6.QtSvgWidgets import QSvgWidget`.
    'PyQt6.QtSvg',
    'PyQt6.QtSvgWidgets',
    'PyQt6.QtNetwork',
    'PyQt6.QtWebChannel',
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebEngineWidgets',
    # Optional features imported inside try/except, so a missing module only
    # disables the feature at runtime instead of failing the build.
    'PyQt6.QtMultimedia',       # audio/video messages, voice notes, camera
    'PyQt6.QtPdf',              # PDF previews
    'PyQt6.QtPositioning',      # location sharing
    'nacl', 'nacl.secret', 'nacl.exceptions',   # encrypted data import
    'segno',                    # QR codes
    'Foundation', 'AppKit', 'objc',             # macOS notifications
]

# 'blink' is included so its widgets (promoted in .ui files and imported
# dynamically) are analysed and their Qt dependencies collected. The others use
# Cython/compiled or lazy imports invisible to static analysis.
for _package in ('application', 'sipsimple', 'eventlib', 'msrplib', 'xcaplib', 'gnutls', 'otr', 'blink'):
    hiddenimports += _submodules(_package)

a = Analysis(
    ['blink-run.py'],
    pathex=[],
    binaries=[],
    # NOTE: do NOT bundle the whole 'macos' folder — it contains the SDK build
    # tree (macos/work, ~300 MB, duplicated into Frameworks) that is never used
    # at runtime. Only the pieces actually needed are added explicitly below.
    datas=[('resources', 'share/blink'), ('blink', 'blink'),
           # blink sets XMLDocument.schema_path = Resources.get('xml-schemas')
           # when frozen, which resolves to share/blink/xml-schemas. Without the
           # schemas there, PIDF/presence XML fails validation at runtime with:
           # "No matching global declaration available for the validation root".
           ('macos/xml-schemas', 'share/blink/xml-schemas')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Dev-only modules pulled in transitively and never used by the app.
    excludes=['tkinter', 'jedi', 'parso', 'IPython'],
    noarchive=False,
    optimize=0,
)

# google-api-python-client ships ~99 MB of static API discovery documents that
# are only a network-fetch cache; drop them from the bundle.
a.datas = [entry for entry in a.datas
           if 'discovery_cache/documents' not in entry[0].replace('\\', '/')]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Blink-Qt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file='macos/Blink.entitlements',
    icon=['macos/blink.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Blink-Qt',
)
app = BUNDLE(
    coll,
    name='Blink-Qt.app',
    icon='macos/blink.icns',
    bundle_identifier='com.ag-projects.blink-qt',
    # macOS refuses camera/microphone/location access (or kills the app)
    # without these usage strings.
    info_plist={
        'NSCameraUsageDescription': 'Required for video calls and video messages',
        'NSMicrophoneUsageDescription': 'Required for audio calls and voice messages',
        'NSLocationWhenInUseUsageDescription': 'Required to share your location in messages',
        'NSLocationUsageDescription': 'Required to share your location in messages',
        'NSContactsUsageDescription': 'Required for calling your contacts',
        'NSHighResolutionCapable': True,
    },
)
