#!/bin/bash
#
# Run the full post-build pipeline on the PyInstaller app:
#   0. remove unused Qt frameworks (QtPdf, QtQuick3D*, QtShaderTools)
#   1. relink system libraries into the bundle
#   2. strip unreferenced / duplicate dylibs
#   3. codesign (hardened runtime)
#   4. notarize, staple, verify, build DMG
#
# Prerequisites: the app is already built with `pyinstaller blink.spec -y`
# (inside the venv). See ./README for signing/notarization credentials.
#
# Usage:  ./run-all.sh
#         SIGN_IDENTITY="Developer ID Application: ..." ./run-all.sh

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"

"$HERE/00-slim-qt.sh"
"$HERE/01-relink.sh"
"$HERE/02-strip.sh"
"$HERE/03-codesign.sh"
"$HERE/04-notarize-dmg.sh"

echo "Pipeline complete."
