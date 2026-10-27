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
if ! python3 -V | grep -qE "3\.(9|10|11|12|13)\b"; then
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
bash "$HERE/install_sipsimple.sh"

# --- Build & install Blink Qt into the same environment --------------------
cd "$REPO"

sudo port install upx

export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"
export PKG_CONFIG_PATH="/opt/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

# Refresh the SDK's Python libraries even when the SDK build was skipped, so
# newer pins (python3-application, msrplib, ...) reach an existing venv.
pip3 install --upgrade pip wheel setuptools
pip3 install -r "$HERE/python-requirements.txt"
pip3 install --no-build-isolation -r "$HERE/sipsimple-requirements.txt"

# Blink's own requirements.
pip3 install -r "$HERE/requirements-osx.txt"

# --- GStreamer (WebRTC video conferences, voice notes as AAC .m4a, video call
# recording) ---------------------------------------------------------------------
# From Homebrew (prebuilt). Not from MacPorts: its plugin sets drag in
# X11/GTK/pulseaudio and build for hours, and a MacPorts gstreamer1 next to
# Homebrew's overwrites its plugin registry (~/.cache/gstreamer-1.0).
if ! command -v brew > /dev/null; then
    echo
    echo "GStreamer (video conferences) needs Homebrew: install it from https://brew.sh"
    echo "and run this script again."
    echo
    exit 1
fi
if port -q installed gstreamer1 2> /dev/null | grep -q .; then
    echo
    echo "MacPorts gstreamer1 is installed and clashes with Homebrew's; remove it with:"
    echo "  sudo port uninstall --follow-dependents gstreamer1 && rm -rf ~/.cache/gstreamer-1.0"
    echo
    exit 1
fi
brew install gstreamer gobject-introspection pkgconf
# nicesrc (webrtcbin's ICE) is a separate formula in Homebrew
brew install libnice-gstreamer 2> /dev/null || brew install libnice || true
BREW_PREFIX=$(brew --prefix)

# PyGObject is built against Homebrew's GLib, never MacPorts' (two GLibs in one
# process crash), so the /opt/local flags exported above are dropped for it.
# pycairo is not used. < 3.51 first: newer PyGObject uses GLib's girepository-2.0,
# which dlopens the bare library names in Homebrew's typelibs
# (libgobject-2.0.0.dylib) and misses /opt/homebrew/lib; Homebrew's
# gobject-introspection (girepository-1.0) looks in its prefix. The venv's python
# is named explicitly: Homebrew's bin goes first in PATH and has a pip3.
VENV_PYTHON="$VENV/bin/python3"
"$VENV_PYTHON" -m pip uninstall -y PyGObject > /dev/null 2>&1 || true
for spec in "PyGObject<3.51" PyGObject; do
    if env -u CFLAGS -u CPPFLAGS -u CPATH \
        PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$(brew --prefix libffi)/lib/pkgconfig" \
        LDFLAGS="-L$BREW_PREFIX/lib" \
        PATH="$BREW_PREFIX/bin:$PATH" \
        "$VENV_PYTHON" -m pip install --no-cache-dir --no-binary PyGObject --no-deps \
            --config-settings=setup-args=-Dpycairo=disabled "$spec"; then
        break
    fi
done

cp "$HERE/_codecs.py" blink/configuration/
cp "$HERE/_tls.py" blink/configuration/

sh build_inplace

# --no-build-isolation so pip builds against the cython/setuptools already in
# this virtualenv (setup.py imports Cython at build time).
pip3 install --no-build-isolation .

# --- Report optional features -------------------------------------------------
(cd / && python3 - <<'PY2'
checks = [
    ('PyQt6.QtMultimedia', 'audio/video messages, voice notes, camera'),
    ('PyQt6.QtPdf', 'PDF previews'),
    ('PyQt6.QtPositioning', 'location sharing'),
    ('nacl.secret', 'encrypted data import'),
    ('segno', 'QR codes'),
    ('Foundation', 'macOS notifications'),
]
for module, feature in checks:
    try:
        __import__(module)
        print('  ok       %-28s %s' % (module, feature))
    except Exception as e:
        print('  MISSING  %-28s %s (%s)' % (module, feature, e))
try:
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init([])
    aac = [n for n in ('avenc_aac', 'fdkaacenc', 'voaacenc') if Gst.ElementFactory.find(n)]
    h264 = [n for n in ('x264enc', 'openh264enc') if Gst.ElementFactory.find(n)]
    print('  ok       %-28s AAC: %s, H.264: %s' % ('GStreamer', aac or 'none (voice notes sent as WAV)', h264 or 'none (no video recording)'))
    from blink.webrtc.videoroom import media_missing
    missing = media_missing()
    if missing:
        print('  MISSING  %-28s video conferences (%s)' % ('GStreamer WebRTC', ', '.join(what for what, package in missing)))
    else:
        print('  ok       %-28s video conferences' % 'GStreamer WebRTC')
except Exception as e:
    print('  MISSING  %-28s not usable: no video conferences, voice notes sent as WAV (%s)' % ('GStreamer', e))
PY2
)

echo
echo "Blink Qt installed into $VENV"
echo "Run it in place with:   sh run"
echo "or from the venv with:  blink"
echo
