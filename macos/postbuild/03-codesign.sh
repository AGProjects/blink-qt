#!/bin/bash
#
# Codesign the app bundle with a hardened runtime, deepest-first.
# Adapted from build_scripts/codesign.sh for the PyInstaller layout
# (Contents/Frameworks + Contents/Resources + nested helper .app bundles).
#
# Run AFTER 01-relink.sh and 02-strip.sh (editing/removing libraries
# invalidates signatures, so signing must come last).
#
# Config (override via environment):
#   SIGN_IDENTITY   signing identity            (default: "Developer ID Application")
#   ENTITLEMENTS    entitlements plist          (default: macos/Blink.entitlements)
#   APP             app bundle to sign          (default: dist/Blink-Qt.app)

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

APP="${APP:-$REPO/dist/Blink-Qt.app}"
SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application}"
ENTITLEMENTS="${ENTITLEMENTS:-$REPO/macos/Blink.entitlements}"

if [ ! -d "$APP" ]; then
    echo "error: $APP not found (build it first)." >&2
    exit 1
fi
if [ ! -f "$ENTITLEMENTS" ]; then
    echo "error: entitlements file $ENTITLEMENTS not found." >&2
    exit 1
fi

# A trusted timestamp needs a real certificate; skip it for ad-hoc ("-") so
# local test signing works.
TS="--timestamp"
[ "$SIGN_IDENTITY" = "-" ] && TS=""

sign() { codesign -f -o runtime $TS -s "$SIGN_IDENTITY" "$@"; }

echo "==> Signing nested libraries (.dylib / .so)"
find "$APP" -type f \( -name '*.dylib' -o -name '*.so' \) -print0 \
    | while IFS= read -r -d '' f; do sign "$f"; done

echo "==> Signing nested helper apps"
# e.g. QtWebEngineCore.framework/.../Helpers/QtWebEngineProcess.app
while IFS= read -r -d '' helper; do
    [ "$helper" = "$APP" ] && continue
    sign "$helper"
done < <(find "$APP" -name '*.app' -print0)

echo "==> Signing frameworks"
while IFS= read -r -d '' fw; do
    sign "$fw"
done < <(find "$APP" -name '*.framework' -type d -print0)

echo "==> Signing the app bundle (with entitlements)"
codesign -f -o runtime $TS \
    --entitlements "$ENTITLEMENTS" \
    -s "$SIGN_IDENTITY" "$APP"

echo "==> Verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "Done. Signed with: $SIGN_IDENTITY"
