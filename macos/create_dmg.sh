#!/bin/bash
#
# Package the PyInstaller-built app bundle into a distributable .dmg.
#
# The bundle is Blink-Qt.app. Note that dist/Blink.app, if present, is the
# separate Cocoa version of Blink and is intentionally NOT packaged here.

set -e

# Always operate from the repository root.
cd "$(dirname "$0")/.."

if ! command -v create-dmg > /dev/null; then
    echo "create-dmg not found. Install it with:  sudo port install create-dmg"
    exit 1
fi

appname="Blink-Qt.app"
volname="Blink-Qt"
app="dist/$appname"
dmg="dist/$volname.dmg"

if [ ! -d "$app" ]; then
    echo "$app not found. Build it first with:  pyinstaller blink.spec -y"
    echo "(dist/Blink.app is the separate Cocoa version, not this one.)"
    exit 1
fi

echo "Packaging $appname into $dmg..."

# Prepare a clean staging folder.
rm -rf dist/dmg
mkdir -p dist/dmg
cp -r "$app" dist/dmg/

# Remove any previous DMG.
test -f "$dmg" && rm "$dmg"

create-dmg \
  --volname "$volname" \
  --volicon "macos/blink.icns" \
  --window-pos 200 120 \
  --window-size 600 300 \
  --icon-size 100 \
  --icon "$appname" 175 120 \
  --hide-extension "$appname" \
  --app-drop-link 425 120 \
  "$dmg" \
  "dist/dmg/"

echo "Created $dmg"
