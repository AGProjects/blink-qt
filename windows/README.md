Blink for Windows
=================

Blink is built on Windows from this repository, with MSYS2, PyInstaller and
Inno Setup. The result is one installer, `dist/Blink-Installer-<version>.exe`.

Windows builds have no QtWebEngine, which MSYS2 does not provide (Chromium does
not build with MinGW). Without it Blink has no server tools window (SIP server
settings, search for people, history on server) and no Google contacts; their
menu items are hidden. Everything else is the same as on Linux.

The environment is MSYS2 **UCRT64**: PyQt6 is not packaged for the older
MINGW64 environment. Everything below runs in the "MSYS2 UCRT64" shell.


## 1. MSYS2

* Install MSYS2 from https://www.msys2.org in a path with no spaces (`C:\msys64`)
  and update it (`pacman -Syu`, twice).
* Open the **MSYS2 UCRT64** shell and install the toolchain:

      pacman -S mingw-w64-ucrt-x86_64-toolchain git patch wget


## 2. SIP SIMPLE SDK

Follow python3-sipsimple's `docs/Install.windows`, with these changes:

* it is written for MINGW64: every package named `mingw-w64-x86_64-...` is
  `mingw-w64-ucrt-x86_64-...` here, and the shell is UCRT64;
* skip mingw-w64-intsafe-headers and mingw-w64-dshow-baseclasses: pjsip 2.17
  carries the DirectShow base classes itself (third_party/BaseClasses);
* `setup_pjsip.py` points at `/mingw64`, so build it with
  `CFLAGS=-I/ucrt64/include LDFLAGS=-L/ucrt64/lib`.

`.github/workflows/windows.yml` (kept here as `windows.yml` until it is moved
there) does all of this on a GitHub Windows runner.

Check it before going on:

    python3 -c "import sipsimple; print(sipsimple.__version__)"


## 3. Blink's dependencies

    pacman -S \
      mingw-w64-ucrt-x86_64-python-pyqt6 \
      mingw-w64-ucrt-x86_64-qt6-multimedia \
      mingw-w64-ucrt-x86_64-qt6-svg \
      mingw-w64-ucrt-x86_64-qt6-pdf \
      mingw-w64-ucrt-x86_64-qt6-positioning \
      mingw-w64-ucrt-x86_64-qt6-websockets \
      mingw-w64-ucrt-x86_64-python-gobject \
      mingw-w64-ucrt-x86_64-gstreamer \
      mingw-w64-ucrt-x86_64-gst-plugins-base \
      mingw-w64-ucrt-x86_64-gst-plugins-good \
      mingw-w64-ucrt-x86_64-gst-plugins-bad \
      mingw-w64-ucrt-x86_64-gst-libav \
      mingw-w64-ucrt-x86_64-libnice \
      mingw-w64-ucrt-x86_64-python-lxml \
      mingw-w64-ucrt-x86_64-python-requests \
      mingw-w64-ucrt-x86_64-python-numpy \
      mingw-w64-ucrt-x86_64-python-pynacl \
      mingw-w64-ucrt-x86_64-pyinstaller

    pip install pgpy sqlobject lxml_html_clean segno

What they are for:

| Package | Used for |
|---|---|
| python-pyqt6, qt6-svg | the user interface and its SVG icons |
| qt6-multimedia | camera, audio playback and recording in the message pane |
| python-gobject, gstreamer, gst-* | voice notes (AAC in .m4a) and media transcoding |
| qt6-pdf | PDF previews in messages (optional) |
| qt6-positioning | location sharing (optional; uses the Windows location service) |
| qt6-websockets, libnice, gst-plugins-bad | video conferences in SylkServer rooms (WebRTC: sylkRTC signalling, ICE, webrtcbin and echo cancellation; optional) |
| python-numpy | video frames |
| python-pynacl | data import from other devices |
| segno | QR codes |
| pyinstaller | building the executable |


## 4. Run from the source tree

    ./run

Blink should start and work before packaging it. Its settings, history and
logs are in `%APPDATA%\Blink`.


## 5. Build the installer

Install Inno Setup 6 (https://jrsoftware.org/isinfo.php) in its default
place, then from the repository root:

    windows/build

This builds `dist/blink/blink.exe` with PyInstaller (`windows/blink.spec`),
then `dist/Blink-Installer-<version>.exe` with Inno Setup
(`windows/setup.iss`). The version is taken from `blink/__info__.py`.

`windows/build --exe` builds only the executable; test it with

    dist/blink/blink.exe

If Inno Setup is installed elsewhere, set `ISCC` to its `ISCC.exe`.


## Files

| File | Purpose |
|---|---|
| `build` | builds the executable and the installer |
| `blink.spec` | PyInstaller: what goes into `dist/blink` (all of it under `lib\` beside `blink.exe`) |
| `setup.iss` | Inno Setup script |
| `distribution/WinSparkle.dll` | the updater (WinSparkle 0.9.4, x64), bundled next to Blink's modules |
| `distribution/psvince.dll` | used by the installer to see whether Blink is running (32-bit, as Inno Setup is) |
| `distribution/bonjoursdksetup.exe` | Apple's Bonjour, needed for Bonjour accounts; offered to administrators who do not have it |
| `distribution/*.bmp`, `license.txt`, `pre_setup.txt` | installer wizard images and texts |
