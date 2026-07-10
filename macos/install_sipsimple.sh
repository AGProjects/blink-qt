#!/bin/bash
#
# Install (or reuse) the SIP SIMPLE client SDK inside the shared virtualenv.
#
# If an importable sipsimple of at least $SIPSIMPLE_MIN_VERSION is already
# present in the active environment, the (expensive) PJSIP build is skipped
# and the SDK is reused as-is.
#
# This mirrors the logic of the python3-sipsimple build scripts: it installs
# pinned, tagged GitHub releases instead of cloning the latest sources via
# darcs.

set -e

SIPSIMPLE_VERSION="5.3.3.1-mac"      # tagged python3-sipsimple release to build
SIPSIMPLE_MIN_VERSION="5.3.2"        # minimum acceptable already-installed version
PJSIP_VERSION="2.11"
SIPCLIENTS_VERSION="5.2.3"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Operate inside the shared virtualenv (create/reuse it if not already active).
if [ -z "$VIRTUAL_ENV" ]; then
    # shellcheck source=/dev/null
    source "$HERE/activate_venv.sh"
fi

# --- Reuse an already-installed SDK ----------------------------------------
if python3 - "$SIPSIMPLE_MIN_VERSION" <<'PY'
import re, sys
try:
    import sipsimple
except Exception:
    sys.exit(1)
def parse(v):
    return [int(x) for x in re.findall(r"\d+", v)]
sys.exit(0 if parse(sipsimple.__version__) >= parse(sys.argv[1]) else 1)
PY
then
    echo "SIP SIMPLE SDK $(python3 -c 'import sipsimple; print(sipsimple.__version__)') already installed - skipping build."
    exit 0
fi

echo "SIP SIMPLE SDK not found (or older than $SIPSIMPLE_MIN_VERSION); building it..."

# --- C build dependencies (MacPorts) ---------------------------------------
echo "Installing port dependencies..."
sudo port install yasm x264 gnutls openssl sqlite3 ffmpeg mpfr libmpc libvpx wget gmp mpc libuuid

# MacPorts' libuuid header conflicts with the macOS SDK one during the build.
if [ -f /opt/local/include/uuid/uuid.h ]; then
    sudo mv /opt/local/include/uuid/uuid.h /opt/local/include/uuid/uuid.h.old
fi

export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"

# --- Python build dependencies (pinned, tagged tarballs) -------------------
echo "Installing python build dependencies..."
pip3 install --upgrade pip
pip3 install -r "$HERE/python-requirements.txt"
pip3 install -r "$HERE/sipsimple-requirements.txt"

# --- Build & install the SDK -----------------------------------------------
BUILD_DIR="$HOME/work"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

srcdir="python3-sipsimple-$SIPSIMPLE_VERSION"
if [ ! -d "$srcdir" ]; then
    echo "Downloading python3-sipsimple $SIPSIMPLE_VERSION..."
    wget -N "https://github.com/AGProjects/python3-sipsimple/archive/refs/tags/$SIPSIMPLE_VERSION.tar.gz"
    tar zxf "$SIPSIMPLE_VERSION.tar.gz"
    rm -f "$SIPSIMPLE_VERSION.tar.gz"
fi

cp "$HERE/_sipsimple_codecs.py" "$srcdir/sipsimple/configuration/_codecs.py"

cd "$srcdir"
echo "Fetching SDK C dependencies (PJSIP $PJSIP_VERSION)..."
chmod +x ./get_dependencies.sh
./get_dependencies.sh "$PJSIP_VERSION"

echo "Building SIP SIMPLE SDK..."
pip3 install .

cd "$BUILD_DIR"

# --- Command line SIP clients (optional companion tools) -------------------
echo "Installing sipclients3 $SIPCLIENTS_VERSION..."
pip3 install "https://github.com/AGProjects/sipclients3/archive/refs/tags/$SIPCLIENTS_VERSION.tar.gz"

echo "SIP SIMPLE SDK installation complete."
