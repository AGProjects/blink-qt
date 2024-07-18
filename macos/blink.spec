# -*- mode: python ; coding: utf-8 -*-

# run pyinstaller pyinstaller.spec --noconfirm
# to build the MacOS app in build/ folder

from PyInstaller.utils.hooks import collect_submodules
all_libraries = [
    "application",
    "PyQt6.QtSvgWidgets"
]

hidden_imports = []
for l in all_libraries:
    hidden_imports += collect_submodules(l)

a = Analysis(
    ['blink-run.py'],
    pathex=[],
    binaries=[],
    datas=[('resources', 'share/blink'), ('blink', 'blink'), ('macos/xml-schemas', 'share/blink/xml-schemas/')],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
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
)
