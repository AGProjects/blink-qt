#!/bin/bash
#
# Notarize the signed app, staple the ticket, verify with Gatekeeper, then
# build the distributable DMG. Adapted from build_scripts/publish/04-06
# (no xcodebuild — the app is already produced by PyInstaller and signed by
# 03-codesign.sh).
#
# Config (override via environment):
#   NOTARY_PROFILE   notarytool keychain profile (default: "notarytool-password")
#   APP              signed app bundle           (default: dist/Blink-Qt.app)
#
# The keychain profile is created once with:
#   xcrun notarytool store-credentials "notarytool-password" \
#       --apple-id <id> --team-id 4DFEFUDLKZ --password <app-specific-password>

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

APP="${APP:-$REPO/dist/Blink-Qt.app}"
NOTARY_PROFILE="${NOTARY_PROFILE:-notarytool-password}"
ZIP="${APP%.app}.zip"

if [ ! -d "$APP" ]; then
    echo "error: $APP not found (build and sign it first)." >&2
    exit 1
fi

echo "==> Zipping for notarization"
/usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"

echo "==> Submitting to notarytool (waits for result)"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait

echo "==> Stapling ticket"
xcrun stapler staple "$APP"

echo "==> Verifying signature and Gatekeeper assessment"
codesign --verify --verbose=2 "$APP"
spctl -a -t exec -vvv "$APP"

rm -f "$ZIP"

echo "==> Building DMG"
"$REPO/macos/create_dmg.sh"

echo "Done."
