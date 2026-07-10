#!/bin/bash
#
# Remove Qt frameworks that Blink-Qt (a Widgets + WebEngine app) does not use:
#   * QtPdf / QtPdfQuick     (+ the libqpdf image-format plugin)
#   * QtQuick3D*             (the whole 3D family)
#   * QtShaderTools
#
# A framework that is still referenced by a surviving binary is KEPT
# automatically (e.g. if QtQuick pulls QtShaderTools), unless you pass --force.
# Run this BEFORE codesigning.
#
# Usage:  ./00-slim-qt.sh            # safe: skip anything still referenced
#         ./00-slim-qt.sh --force    # remove regardless (verify the app after!)
#         APP=/path/to/Foo.app ./00-slim-qt.sh

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
APP="${APP:-$REPO/dist/Blink-Qt.app}"
QT="$APP/Contents/Frameworks/PyQt6/Qt6"

if [ ! -d "$APP" ]; then
    echo "error: $APP not found (build it first)." >&2
    exit 1
fi

echo "==> Removing unused Qt frameworks from $APP"
python3 "$HERE/bundle_tool.py" slim "$APP" \
    --framework 'QtPdf' \
    --framework 'QtPdfQuick' \
    --framework 'QtQuick3D*' \
    --framework 'QtShaderTools' \
    "$@"

# Associated data/plugins that only exist to serve the removed frameworks.
echo "==> Removing associated QML modules and plugins"
for extra in \
    "$QT/qml/QtQuick3D" \
    "$QT/plugins/imageformats/libqpdf.dylib"
do
    if [ -e "$extra" ]; then
        sz=$(du -sh "$extra" 2>/dev/null | cut -f1)
        rm -rf "$extra"
        echo "  removed  ${extra#$APP/}  ($sz)"
    fi
done

echo "==> Verifying nothing surviving still links the removed frameworks"
missing=0
while IFS= read -r -d '' m; do
    if otool -L "$m" 2>/dev/null | grep -qE '/(QtPdf|QtPdfQuick|QtQuick3D[A-Za-z]*|QtShaderTools)\.framework/'; then
        echo "  DANGLING ref in ${m#$APP/}" >&2
        missing=1
    fi
done < <(find "$APP" -type f \( -name '*.dylib' -o -name '*.so' \) -print0)

if [ "$missing" -ne 0 ]; then
    echo "WARNING: some surviving binaries still reference a removed framework." >&2
    echo "         Re-run without --force, or rebuild; do NOT ship this bundle." >&2
    exit 1
fi
echo "Done. Launch the app and confirm it still works before signing."
