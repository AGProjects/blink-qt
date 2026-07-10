#!/bin/bash

# generate a specs file used by pyinstaller to generate a valid app for MacOS
# the file must be tweaked, see the modified blink.specs file

# Note: do NOT add-data the whole macos/ folder — it drags in the SDK build
# tree (macos/work). Only bundle what the app needs. The discovery_cache
# documents (~99 MB) still need trimming afterwards; see blink.spec.
pyinstaller --add-data resources:share/blink --add-data blink:blink \
--add-data macos/xml-schemas:share/blink/xml-schemas \
--hidden-import=application \
--hidden-import=PyQt6.QtSvg --hidden-import=PyQt6.QtSvgWidgets \
--hidden-import=PyQt6.QtNetwork --hidden-import=PyQt6.QtWebChannel \
--hidden-import=PyQt6.QtWebEngineCore --hidden-import=PyQt6.QtWebEngineWidgets \
--collect-submodules application --collect-submodules sipsimple \
--collect-submodules eventlib --collect-submodules msrplib --collect-submodules xcaplib \
--collect-submodules gnutls --collect-submodules otr --collect-submodules blink \
--exclude-module tkinter --exclude-module jedi --exclude-module parso --exclude-module IPython \
 --osx-bundle-identifier com.ag-projects.blink-qt --osx-entitlements-file macos/Blink.entitlements \
--icon=macos/blink.icns -n 'Blink-Qt' --windowed --argv-emulation blink-run.py
