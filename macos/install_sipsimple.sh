#!/bin/bash
#
# Install (or reuse) the SIP SIMPLE client SDK inside the shared virtualenv.
#
# If an importable sipsimple of at least $SIPSIMPLE_MIN_VERSION is already
# present in the active environment, the (expensive) PJSIP build is skipped.
#
# Otherwise the SDK is built by python3-sipsimple's own mac/ scripts, from a
# checkout at $SIPSIMPLE_SRC (default ~/work/python3-sipsimple; cloned from
# GitHub if absent). Those scripts own the hard parts: universal MacPorts
# libraries, ffmpeg pinned to 4.4.5 (pjmedia does not build against ffmpeg 5+,
# so never `port install ffmpeg` here), optional bcg729, and PJSIP 2.17.
#
# Knobs:
#   SIPSIMPLE_SRC   python3-sipsimple checkout   (default ~/work/python3-sipsimple)
#   PJSIP_VERSION   2.17 (default) or 2.12
#   FORCE_SDK=1     rebuild even if a sufficient SDK is installed

set -e

SIPSIMPLE_MIN_VERSION="5.6.0"       # matches debian/control
SIPSIMPLE_GIT="https://github.com/AGProjects/python3-sipsimple.git"
SIPSIMPLE_SRC="${SIPSIMPLE_SRC:-$HOME/work/python3-sipsimple}"
SIPCLIENTS_SRC="$(dirname "$SIPSIMPLE_SRC")/sipclients3"
export PJSIP_VERSION="${PJSIP_VERSION:-2.17}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "$VIRTUAL_ENV" ]; then
    # shellcheck source=/dev/null
    source "$HERE/activate_venv.sh"
fi

# --- Reuse an already-installed SDK ----------------------------------------
if [ "${FORCE_SDK:-0}" != "1" ] && (cd / && python3 - "$SIPSIMPLE_MIN_VERSION") <<'PY'
import re, sys
try:
    import sipsimple
    import sipsimple.core._core  # noqa: F401  (a broken/ABI-mismatched build counts as missing)
except Exception:
    sys.exit(1)
def parse(v):
    return [int(x) for x in re.findall(r"\d+", v)][:3]
sys.exit(0 if parse(sipsimple.__version__) >= parse(sys.argv[1]) else 1)
PY
then
    echo "SIP SIMPLE SDK $(cd / && python3 -c 'import sipsimple; print(sipsimple.__version__)') already installed - skipping build."
    exit 0
fi

echo "SIP SIMPLE SDK missing, broken or older than $SIPSIMPLE_MIN_VERSION; building it..."

# --- Get the SDK source -----------------------------------------------------
if [ ! -f "$SIPSIMPLE_SRC/setup.py" ]; then
    echo "Cloning python3-sipsimple into $SIPSIMPLE_SRC..."
    mkdir -p "$(dirname "$SIPSIMPLE_SRC")"
    git clone "$SIPSIMPLE_GIT" "$SIPSIMPLE_SRC"
fi

MAC="$SIPSIMPLE_SRC/mac"
if [ ! -x "$MAC/04-install_sipsimple.sh" ] && [ ! -f "$MAC/04-install_sipsimple.sh" ]; then
    echo "$MAC/04-install_sipsimple.sh not found - $SIPSIMPLE_SRC is too old, update it."
    exit 1
fi
chmod +x "$MAC"/*.sh

# Blink-specific codec defaults for the SDK.
cp "$HERE/_sipsimple_codecs.py" "$SIPSIMPLE_SRC/sipsimple/configuration/_codecs.py"

# --- Build ------------------------------------------------------------------
"$MAC/01-install-build-env.sh"
"$MAC/02-install-c-deps.sh"
"$MAC/02b-install-bcg729.sh"            # fail-soft: no G.729 if it can't build

export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"
pip3 install --upgrade pip wheel setuptools
pip3 install -r "$HERE/python-requirements.txt"
pip3 install --no-build-isolation -r "$HERE/sipsimple-requirements.txt"

"$MAC/04-install_sipsimple.sh" --version "$PJSIP_VERSION"

# --- Command line SIP clients (optional) -----------------------------------
if [ -f "$SIPCLIENTS_SRC/setup.py" ]; then
    "$MAC/05-install_sipclients.sh"
else
    echo "sipclients3 not found at $SIPCLIENTS_SRC - skipping the sip-* CLI tools."
fi

echo "SIP SIMPLE SDK installation complete."
