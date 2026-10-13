# Blink Qt — screen sharing as a video device

Status as of 2026-10-13: implemented (portal + PipeWire only, decision 1 below), with the remote pointer and the screen request handshake of Blink for macOS and Sylk Mobile. See "Implementation" at the end.

## Why

Blink Qt shares the desktop over VNC: `x11vnc` captures the screen and the RFB stream travels over MSRP. `x11vnc` only works on an X11 session.

- Ubuntu 26.04 (resolute) ships GNOME with no Xorg session (dropped in 25.10), so sharing *out* fails there. Viewing a remote screen with the built-in RFB viewer still works.
- On noble, bookworm and trixie the default GNOME session is already Wayland; users can still choose an X11 session, but that option is going away.

## What Blink for macOS does

Screen sharing is a video capture device, implemented in python3-sipsimple as `deps/patches/2.17/32_avf_screen_capture.patch`:

- `avf_dev.m` lists every active display after the cameras: "My screen" (main display), "My screen 2", …
- Picking one as the camera sends the screen instead of the camera. Nothing changes above pjmedia: the call is an ordinary video call, so it interoperates with any client (Sylk Mobile, WebRTC, other SIP phones).
- Capture uses ScreenCaptureKit (macOS 12.3+), weak-linked; older systems list no screen devices.
- Needs the Screen Recording permission. Without it the first open triggers the system prompt and fails with `PJMEDIA_EVID_NOTREADY`, and the application falls back to a camera.
- ScreenCaptureKit only delivers a frame when the screen changes, while the packetizer advances the RTP timestamp by a fixed amount per frame. The callback keeps only the latest buffer and a timer at the configured frame rate hands it to pjmedia. This keeps timing right and lets keyframe requests be served on a static screen.
- Output is BGRA at the requested size, letterboxed.

## Proposed Linux design

The same shape: a new pjmedia capture device next to `v4l2_dev.c`, in python3-sipsimple, exposed as "My screen" in the camera list.

### Capture backend

1. **xdg-desktop-portal ScreenCast + PipeWire** (primary). The only method that works on Wayland; GNOME and KDE also offer it on X11 sessions.
   - D-Bus handshake with `org.freedesktop.portal.ScreenCast`: CreateSession → SelectSources → Start → OpenPipeWireRemote, giving a PipeWire fd and node id.
   - Consume the node with libpipewire. Ask for shared-memory buffers (MemFd/MemPtr, BGRx/RGBx), not DMA-BUF, to avoid GPU import.
2. **XShm on X11** (optional fallback) for sessions without a portal backend, e.g. bare window managers. Can list monitors through XRandR the way macOS lists displays.

### Carried over from the macOS patch

- Keep only the latest PipeWire buffer; a timer at the configured frame rate hands it to pjmedia (same reason as ScreenCaptureKit: PipeWire delivers on damage only).
- Scale and letterbox to the requested size with libswscale (already linked through ffmpeg).
- First open returns `PJMEDIA_EVID_NOTREADY` while the user is being asked, and Blink falls back to the camera.

### Differences from macOS

| | macOS | Linux (portal) |
|---|---|---|
| Permission | Screen Recording, granted once in System Settings | Portal picker; restore token (ScreenCast v4, `persist_mode=2`) skips it on later calls |
| Device list | One device per display | One "My screen"; the picker chooses the monitor (or the XShm fallback lists monitors) |
| What can be shared | Whole display | Whole monitor or a single window |
| First use | Fails with NOTREADY, system prompt | Waits on the picker; return NOTREADY and complete in the background |

The restore token has to be stored, e.g. as a python3-sipsimple setting, so later calls go straight through.

## Changes needed

python3-sipsimple:

- New `screen_dev.c` in `pjmedia-videodev` (roughly the size of patch 32), as a new numbered patch in `deps/patches/2.17`
- Makefile / aconfigure gate and a `PJMEDIA_VIDEO_DEV_HAS_…` define in `setup_pjsip.py` (Linux only)
- Build-Depends: `libpipewire-0.3-dev`, `libglib2.0-dev` (GDBus for the portal); for the XShm fallback also `libx11-dev`, `libxext-dev`, `libxrandr-dev`. All available on bookworm, trixie, noble and resolute.

Blink Qt:

- Little or nothing: the device appears in the camera list on its own
- Fall back to the camera on `PJMEDIA_EVID_NOTREADY`
- Store the portal restore token
- Keep `x11vnc` and VNC desktop sharing for remote control on X11; screen-as-video is the way to share on Wayland

Runtime: PipeWire and a portal backend (`xdg-desktop-portal-gnome`, `-kde` or `-wlr`), which the desktop already provides.

## Decision

1. Portal + PipeWire only (chosen)
2. Portal + PipeWire with the XShm fallback

## Implementation

### python3-sipsimple

`deps/patches/2.17/52_linux_screen_capture.patch` adds `pjmedia/src/pjmedia-videodev/screen_dev.c` and `screen_dev.h`, registers the factory after the cameras and builds `screen_dev.o` everywhere (stubs where the device is not built).

- Built when `setup_pjsip.py` finds `libpipewire-0.3`, `gio-2.0` and `gio-unix-2.0` with pkg-config (`PJMEDIA_VIDEO_DEV_HAS_SCREEN=1`, CFLAGS/LIBS for configure, libraries added to the extension) and FFmpeg's libswscale is in. `SIPSIMPLE_DISABLE_SCREEN_CAPTURE=1` leaves it out. Build-Depends: `libpipewire-0.3-dev`, `libglib2.0-dev`. An existing build tree needs a clean build (configure runs once).
- One device, "My screen", listed when the session bus has a ScreenCast portal that can capture monitors.
- Opening the device never asks the user. `pjmedia_screen_dev_request()` starts the handshake on a thread of its own (CreateSession, SelectSources with `types=MONITOR`, `cursor_mode=EMBEDDED`, `persist_mode=2` and the restore token, Start, OpenPipeWireRemote); the state goes PENDING, then READY (or DENIED/FAILED). The application selects the device on READY; opening it at any other time is `PJMEDIA_EVID_NOTREADY` and the SDK falls back to a camera, so a "My screen" left in the settings never shows the picker at start. A READY session nobody opens is closed after 30 s.
- Monitors only, not windows: a remote pointer has to land on a known place.
- PipeWire: `MemFd`/`MemPtr` buffers only, `DONT_RECONNECT` so that sharing stopped from the desktop's indicator ends the stream (state ENDED) instead of waiting forever. The newest buffer is held (dropped on `remove_buffer`), a pjmedia clock at the configured rate converts it with libswscale into letterboxed I420 at the requested size and hands it to pjmedia. The PipeWire thread registers with pjlib before logging.
- Python (`sipsimple.core`): `screen_capture_info()` (state, monitor position/size, source size), `request_screen_capture()`, `reset_screen_capture()`, `screen_capture_restore_token()`, `set_screen_capture_restore_token()`. `CORE_BUILD` is 18.

Tested in a container against PipeWire with a synthetic video node and a stand-in portal: picker answered, denied and abandoned; restore token sent and renewed; session closed after use; producer disappearing mid-capture (no crash, state ENDED); 15 fps of letterboxed frames from a static 1920x1200 source.

### Blink Qt

- `blink/screen_pointer.py`: wire format and geometry (the same messages as Blink for macOS and Sylk Mobile, see the module docstring), tested in `tests/test_screen_pointer.py`.
- `blink/screen_share.py`, `ScreenShareManager`: capabilities at call start, `start`/`stop` when our video is/stops being the screen, the request handshake (a prompt when asked; accepting shares the screen), pointers drawn on the shared monitor with a click-through overlay window and acked, the desktop's answer polled while sharing. The restore token and the camera to go back to are kept in QSettings. The camera comes back when no call with video is left, when sharing is stopped from the desktop, or when a camera is picked. Capabilities are not sent to phone numbers and conferences.
- Video window: "My screen" in the Camera menu shares the screen (after the desktop's picker), "Request Screen" asks the other party for theirs, a Pointer button shows while the other party shares a screen it draws pointers on (click the picture to point; Esc leaves pointer mode), the acked click is echoed with a green ring. The local picture is not mirrored while it is the screen. The screen is not offered as a default camera in Preferences or the main window's Video menu.
- The screen is sent at 1280x720 and 1.5 Mbps while it is shared, cameras at the video settings: selecting "My screen" also sets the capture resolution, and the calls going on reopen their encoder at the new size without renegotiating (`VideoStream.set_encoder_size()`, patch `53_vid_stream_set_enc_size.patch`); the other side gets a keyframe at the new size. Going back to a camera restores both.
- The VNC entry points (main window screen button, contact and call menus, MSRP sessions window) are hidden behind `VNC_SCREEN_SHARING_OFFERED` in `blink/screensharing/support.py`; incoming VNC offers are still handled.
- `x11vnc`/VNC desktop sharing over MSRP stays as it was (remote control, X11 only).
