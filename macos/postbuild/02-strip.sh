#!/bin/bash
#
# Remove unreferenced / duplicate loose dylibs from Contents/Frameworks.
# Pruned files are MOVED to <dist>/pruned-libs (not deleted), so the app can be
# restored if a removal turns out to be wrong.
#
# IMPORTANT: run 01-relink.sh FIRST, so the reference graph is accurate.
#
# Usage:  ./02-strip.sh                 # prune (default, per project policy)
#         ./02-strip.sh --dry-run       # report only, move nothing
#         APP=/path/to/Foo.app ./02-strip.sh

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
APP="${APP:-$REPO/dist/Blink-Qt.app}"

echo "==> Stripping unreferenced dylibs from $APP"
python3 "$HERE/bundle_tool.py" strip "$APP" "$@"

echo
echo "Tip: launch the app and exercise every feature (audio, video, chat,"
echo "screen sharing, web views) before signing. If something fails to load,"
echo "restore the library from $REPO/dist/pruned-libs and re-run with --dry-run."
