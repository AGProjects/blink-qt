# Blink Qt — screen sharing as a video device

Status as of 2026-10-09: findings and proposed design, nothing implemented yet.

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

## Open decision

1. Portal + PipeWire only (recommended)
2. Portal + PipeWire with the XShm fallback
