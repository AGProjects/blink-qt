#!/bin/bash

# generate a specs file used by pyinstaller to generate a valid app for MacOS
# the file must be tweaked, see the modified blink.specs file

pyinstaller --add-data macos:macos --add-data resources:share/blink --add-data blink:blink \
--hidden-import=application \
 --osx-bundle-identifier com.ag-projects.blink-qt --osx-entitlements-file macos/Blink.entitlements \
--icon=macos/blink.icns -n 'Blink' --windowed --argv-emulation blink-run.py
