# AG Podcast Mixer link: what is still to migrate

Status as of 2026-11-14. The protocol, the SDK device and the ring layout are in python3-sipsimple
`docs/PodcastMixerLink.md`; the jamming / interview set-up as it is today on Linux is in
`docs/jamming-ag-cutemusic.md`.

## What the link is

AG CuteMusic's mixer and Blink exchange 48 kHz stereo PCM through two shared-memory rings in a per-user directory
(`$XDG_RUNTIME_DIR/ag-podcast-mixer` on Linux, `$TMPDIR/ag-podcast-mixer` on macOS):

- `to-call.ring`: AG CuteMusic's mix (the mic, the music with TO CALLS).
- `from-call.ring`: what Blink plays (the other party), for the BLINK strip.

The SDK turns the rings into an ordinary audio device, "AG Podcast Mixer" (pjsip patch
`deps/patches/2.17/63_agpm_audio_device.patch`). The device is listed only while AG CuteMusic holds `mixer.lock`.
No sound server modules, no ALSA configuration, no driver, no root.

## Done

| Part | State |
|------|-------|
| python3-sipsimple | Device factory (patch 63), `Engine.podcast_mixer_device` / `podcast_mixer_available` / `podcast_mixer_dir`, `tools/agpm.py` (the Python ring module, canonical copy), `tools/agpm-test.py` (a fake mixer). |
| AG CuteMusic | `cutemusic/link.py`, `cutemusic/agpm.py`. The mic and TO CALLS go to `to-call`, the BLINK strip plays `from-call`, `mixer.lock` is held while the mixer is on. share.py (PulseAudio / ALSA) runs next to it on Linux. Works on macOS (osxaudiosrc / osxaudiosink). |
| Blink for macOS | `CuteMusic.py`: the device is refreshed as AG CuteMusic starts and quits (no restart), music mode (echo cancelling off, 48 kHz, stereo, the settings before put back), `blink.lock`, `calls.json`. Never switched to on its own. Stereo calls (`StereoCalls.py`). |
| Tested | AG CuteMusic → Blink for macOS → stereo SIP call → Blink Qt and back, on macOS, 2026-11-14. |

## Still to do: 1-to-1 SIP calls

### Blink Qt (phase 3)

1. **`blink/cutemusic.py` on the link directory.**
   - `running()`: `mixer.lock` in the link directory, or `engine.podcast_mixer_available`, replacing
     `$XDG_RUNTIME_DIR/ag-cute-music/mixer`.
   - `CALLS`: `calls.json` in the link directory, replacing `$XDG_RUNTIME_DIR/blink/calls.json`. AG CuteMusic already
     reads the link directory first.
   - `blink.lock` held in the link directory while Blink runs. AG CuteMusic tests it (and still `~/.blink/lock`,
     which this Blink no longer has: its data directory is `~/.blink-qt/`).
2. **Refresh, no restart.** `Watcher`: when AG CuteMusic starts or quits, `SIPApplication.engine.refresh_sound_devices()`
   before the menus are rebuilt. The device then comes and goes at runtime.
   - Gone: the "restart Blink" case in `unavailable_reason()` / `unavailable_hint()`.
   - Gone: AG CuteMusic's "quit and start Blink again" message.
3. **The duplicate device on Linux.** With the new SDK, Blink Qt lists two "AG Podcast Mixer" devices: the ALSA PCM
   set up by share.py (`/etc/alsa/conf.d/60-ag-cute-music.conf`, or `~/.config/alsa/ag-cute-music.conf`) and the
   SDK's. Blink chooses a device by name and pjmedia lists ALSA's first, so the old path would win. To decide:
   - (a) Blink Qt leaves the ALSA one out of its device lists when the SDK's device is there;
   - (b) AG CuteMusic stops advertising the ALSA device (its `hint`) while the link runs, keeping share.py's modules
     for older Blinks. This is the simpler option.
4. **Music mode** (`apply_music_mode()`) unchanged: it already keys on the device name.
5. **AG CuteMusic's BLINK strip** follows: once Blink Qt uses the SDK's device, `window.py` plays `from-call` on Linux
   too, not share.py's sink monitor. Today it chooses share.py's whenever share.py's devices are up.

### Blink for macOS

6. **Devices menu and Preferences**, as in Blink Qt:
   - AG CuteMusic running: "AG Podcast Mixer" listed first.
   - AG CuteMusic installed but not running: listed first, greyed out, with the reason ("start AG CuteMusic").
   - AG CuteMusic not installed: not listed at all.

   Installed means the bundle `com.ag-projects.ag-cute-music` is known to Launch Services
   (`NSWorkspace URLForApplicationWithBundleIdentifier:`). AG CuteMusic run from its source tree counts only while
   it runs.
7. **Underruns and drops in the Activity log.** The device's capture underruns and dropped playback frames are logged
   by pjmedia (level 4, the PJSIP log) when a call ends. They are wanted in the Activity log; that needs a small SDK
   accessor for the last stream's counts.
8. **Redial of a stereo call stays stereo.** Today Redial calls the address again, in mono.

### AG CuteMusic

9. **The Blink lock it tests:** `blink.lock` in the link directory (done in `link.py` / `window.py`); the old
   `~/.blink/lock` test can go once Blink Qt holds `blink.lock`.

## Later: conferences and rooms

- **Blink Qt WebRTC rooms** (`blink/webrtc/videoroom.py`): `appsrc` reading `to-call` and `appsink` writing
  `from-call` (`agpm.py` copied into `blink/`), replacing `pulsesrc device=cutemusic_mixer_out` /
  `pulsesink device=cutemusic_mixer_in`.
- **Blink for macOS WebRTC rooms:** libwebrtc does not use pjmedia. An Objective-C class implementing
  WebRTC.framework's `RTCAudioDevice` protocol (the framework has
  `initWithEncoderFactory:decoderFactory:audioDevice:`) feeds the room from `to-call` and writes `from-call`. The
  room's factory is made with it when AG Podcast Mixer is chosen. Music mode in rooms: no echo cancelling,
  stereo, 128 kbit/s.
- **SIP conferences** (the MSRP window with audio) already work: their audio is a SIP call through the SDK device.
  Connect with Stereo Audio is there for room contacts.

## Later: other

- **Blink Pro (sandboxed):** its `$TMPDIR` is inside its container, so it cannot see AG CuteMusic's directory. This
  needs a shared app group (`com.apple.security.application-groups`) and the link directory in the group container
  when it exists (SDK patch 63, `agpm.py`, both clients), or Blink Pro stays out of the link.
- **Linux cleanup (phase 7):** once Blink Qt is on the link, remove share.py's pactl modules, the ALSA PCM and
  `alsa/60-ag-cute-music.conf` from the package, along with the pipewire-alsa / libasound2-plugins dependencies.
- **Windows:** the device factory is POSIX only (mmap, flock); Windows needs named shared memory and a named
  mutex.
- **Latency for jamming:** the SDK device moves the conference bridge's 20 ms frames. Less needs a shorter bridge
  ptime and Opus frame size (an SDK change).
