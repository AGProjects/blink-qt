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
chmod +x "$HERE/install_sipsimple.sh"
"$HERE/install_sipsimple.sh"

# --- Build & install Blink Qt into the same environment --------------------
cd "$REPO"

sudo port install libvncserver upx

export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"
export PKG_CONFIG_PATH="/opt/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

# Refresh the SDK's Python libraries even when the SDK build was skipped, so
# newer pins (python3-application, msrplib, ...) reach an existing venv.
pip3 install --upgrade pip wheel setuptools
pip3 install -r "$HERE/python-requirements.txt"
pip3 install --no-build-isolation -r "$HERE/sipsimple-requirements.txt"

# Blink's own requirements (cython + setuptools for the _rfb extension).
pip3 install -r "$HERE/requirements-osx.txt"

# --- Optional: GStreamer (voice notes as AAC .m4a, video call recording) -----
# Without it Blink still runs: voice notes are sent as WAV and video recording
# is disabled. Opt in with WITH_GSTREAMER=1. gst-libav is deliberately not
# installed: it pulls a current ffmpeg, which would replace the ffmpeg 4.4.5
# that PJSIP is built against.
if [ "${WITH_GSTREAMER:-0}" = "1" ]; then
    sudo port install gobject-introspection gstreamer1 gstreamer1-gst-plugins-base \
        gstreamer1-gst-plugins-good gstreamer1-gst-plugins-ugly gstreamer1-gst-plugins-bad
    pip3 install PyGObject || pip3 install "PyGObject<3.51"
fi

cp "$HERE/_codecs.py" blink/configuration/
cp "$HERE/_tls.py" blink/configuration/

chmod +x ./build_inplace ./run ./bin/blink ./blink-run.py
./build_inplace

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
    ('blink.screensharing._rfb', 'screen sharing'),
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
    Gst.init(None)
    aac = [n for n in ('avenc_aac', 'fdkaacenc', 'voaacenc') if Gst.ElementFactory.find(n)]
    h264 = [n for n in ('x264enc', 'openh264enc') if Gst.ElementFactory.find(n)]
    print('  ok       %-28s AAC: %s, H.264: %s' % ('GStreamer', aac or 'none (voice notes sent as WAV)', h264 or 'none (no video recording)'))
except Exception:
    print('  optional %-28s not installed (WITH_GSTREAMER=1 to add)' % 'GStreamer')
PY2
)

echo
echo "Blink Qt installed into $VENV"
echo "Run it in place with:   ./run"
echo "or from the venv with:  blink"
echo
