#!/bin/bash
#
# Install Blink Qt and the SIP SIMPLE SDK on macOS into a dedicated
# virtualenv (~/work/sipsimple-python-<ver>-<arch>-env).
#
# The SIP SIMPLE SDK is reused if it is already installed in that environment,
# so re-running this script does not recompile PJSIP every time.

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# --- Preconditions ---------------------------------------------------------
if ! python3 -V | grep -qE "3\.(9|10|11|12|13)"; then
    echo
    echo "Please install Python 3.9 or newer from https://www.python.org/"
    echo
    exit 1
fi

if ! command -v port > /dev/null; then
    echo
    echo "Please install MacPorts from https://www.macports.org"
    echo
    exit 1
fi

# --- Create/reuse the shared virtualenv ------------------------------------
# shellcheck source=/dev/null
source "$HERE/activate_venv.sh"

# --- Install or reuse the SIP SIMPLE SDK -----------------------------------
# install_sipsimple.sh self-detects an already-installed SDK and skips the
# expensive build when possible. It inherits the active virtualenv via the
# exported VIRTUAL_ENV / PATH set above.
chmod +x "$HERE/install_sipsimple.sh"
"$HERE/install_sipsimple.sh"

# --- Build & install Blink Qt into the same environment --------------------
cd "$REPO"

sudo port install libvncserver upx

export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"

# requirements-osx.txt includes cython and wheel so that both build_inplace
# and the local install below can compile the blink.screensharing._rfb Cython
# extension even when the SDK build (which also installs cython) was skipped.
pip3 install -r "$HERE/requirements-osx.txt"

cp "$HERE/_codecs.py" blink/configuration/
cp "$HERE/_tls.py" blink/configuration/

chmod +x ./build_inplace ./run ./bin/blink ./blink-run.py
./build_inplace

# --no-build-isolation so pip builds against the cython/setuptools already in
# this virtualenv (setup.py imports Cython at build time, which pip's isolated
# build environment would otherwise not provide).
pip3 install --no-build-isolation .

echo
echo "Blink Qt installed into $VENV"
echo "Run it in place with:   ./run"
echo "or from the venv with:  blink"
echo
