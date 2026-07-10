#!/bin/bash
#
# Relink absolute system-library load commands (/opt/local, /usr/local, ...) in
# the built app to bundle-relative paths, copying any missing libraries in, so
# the .app is self-contained. Then verify none remain.
#
# Usage:  ./01-relink.sh              # dist/Blink-Qt.app
#         APP=/path/to/Foo.app ./01-relink.sh

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
APP="${APP:-$REPO/dist/Blink-Qt.app}"

echo "==> Relinking system libraries in $APP"
python3 "$HERE/bundle_tool.py" relink "$APP"

echo "==> Verifying"
python3 "$HERE/bundle_tool.py" verify "$APP"
