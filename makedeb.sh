#!/bin/bash
set -e

# skip dh_auto_test during the build (like autopackager does)
export DEB_BUILD_OPTIONS=nocheck

distro="${1:-}"
mode="${2:-}"

# rebuild mode: reuse the extracted build tree from a previous run;
# skips clean (-nc) and builds binary packages only (-b)
if [ "$mode" = "rebuild" ]; then
    cd dist/*/
    if [ -n "$distro" ] && [ "$distro" != "sid" ]; then
        sed -i "s/) unstable/$distro) $distro/" debian/changelog
        head -1 debian/changelog
    fi
    debuild --no-sign -nc -b
    exit 0
fi

sudo mk-build-deps --install --root-cmd sudo --remove debian/control

rm -rf dist build

if [ -f setup.py ]; then
    python3 setup.py sdist
else
    python3 -m build --sdist
fi
cd dist

tar zxf *.tar.gz
cd */

# add distro suffix to the changelog version (like autopackager does),
# only in the extracted build tree - never in the source repo
if [ -n "$distro" ] && [ "$distro" != "sid" ]; then
    sed -i "s/) unstable/$distro) $distro/" debian/changelog
    head -1 debian/changelog
fi

debuild --no-sign
