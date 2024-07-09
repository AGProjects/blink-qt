#!/bin/sh
# Create a folder (named dmg) to prepare our DMG in (if it doesn't already exist).
mkdir -p dist/dmg
# Empty the dmg folder.
rm -r dist/dmg/*
# Copy the app bundle to the dmg folder.
cp -r "dist/Blink-Qt.app" dist/dmg
# If the DMG already exists, delete it.
test -f "dist/Blink-Qt.dmg" && rm "dist/Blink-Qt.dmg"
create-dmg \
  --volname "Blink-Qt" \
  --volicon "macos/blink.icns" \
  --window-pos 200 120 \
  --window-size 600 300 \
  --icon-size 100 \
  --icon "Blink-Qt.app" 175 120 \
  --hide-extension "Blink-Qt.app" \
  --app-drop-link 425 120 \
  "dist/Blink-Qt.dmg" \
  "dist/dmg/"
  