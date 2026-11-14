# Blink Qt — AG Podcast Mixer as a sound device: jamming and live interviews

Status as of 2026-11-14: implemented in AG CuteMusic (`~/work/ag-cute-music`) and in Blink Qt (`blink/cutemusic.py`,
`blink/mainwindow.py`, `blink/preferences.py`, `blink/webrtc/videoroom.py`). It has not yet been tested end to end on
PipeWire.

## Who it is for

- **Musicians jamming together over a call:** each one hears the others in their headset, mixed with their own
  instrument or voice and, if they like, a backing track, and sends their part at music quality, not voice quality.
- **Hosts of live interviews and podcasts:** the host's microphone, jingles and music beds go out as one
  programme, and the guests come back on their own fader, never fed back to themselves.
- **Karaoke with friends:** a backing track with VOCAL CUT, the singer's microphone with echo.

All three are the same set-up. The person mixing uses AG CuteMusic and Blink; the others join with any SIP client or
from a video room.

## What it does

- AG CuteMusic's mixer appears in Blink as one sound device, **AG Podcast Mixer**, for both input and output. It
  is listed only while AG CuteMusic is running.
- Choosing it in Blink's Devices menu is the only setup. Every call (1-to-1 SIP, SIP conference, WebRTC video room)
  then:
  - sends the mix: the microphone or instrument (the MIC strip, with echo if wanted), plus the music, backing track
    or jingles while TO CALLS is on;
  - plays the others in the call (bandmates, guests) into the mixer's **BLINK** strip, heard in the headset with
    its own fader;
  - is set up for music: echo cancelling off and 48 kHz for SIP; stereo, untouched, Opus at 128 kbit/s in rooms.
- The others never get their own sound back: what Blink plays never goes into what it sends.

## Audio flow

```
                         AG CuteMusic                                          sound server
 mic (pulsesrc) ──────► tee ──► pulsesink ──────────────────────────────────►  output (headset)
                         └────► pulsesink device=cutemusic_mixer_bus ──┐
 music (playbin) ─────► tee ──► pulsesink ──────────────────────────────────►  output (headset)
          (TO CALLS on)  └────► pulsesink device=cutemusic_mixer_bus ──┤
                                                                      ▼
                                  module-null-sink    cutemusic_mixer_bus
                                  module-remap-source cutemusic_mixer_out ─────┐
                                                                               │ capture
                                  ALSA pcm.ag_cutemusic_mixer (pipewire)       │ ◄── Blink records ("AG Podcast Mixer")
                                                                               │ playback
                                  module-null-sink    cutemusic_mixer_in ◄─────┘ ◄── Blink plays
                                         │ .monitor
 BLINK strip (pulsesrc ! volume ! level ! pulsesink) ◄┘ ──────────────────────►  output (headset)
```

## AG CuteMusic side (`cutemusic/share.py`)

- **When it starts:** loads three modules with `pactl`:
  - `module-null-sink sink_name=cutemusic_mixer_bus`;
  - `module-remap-source source_name=cutemusic_mixer_out` on that sink's monitor;
  - `module-null-sink sink_name=cutemusic_mixer_in`.

  Modules left behind by a CuteMusic that crashed are unloaded first.
- **While it runs:** holds an exclusive `flock` on `$XDG_RUNTIME_DIR/ag-cute-music/mixer`. Blink takes that as "CuteMusic
  is running". The lock goes with the process, so a crash does not leave the device listed.
- **When it quits:** unloads the modules and removes the file.
- **Mixer view:**
  - **MIC strip:** INPUT, ECHO, fader and meter. The mic always goes to the headset and to `cutemusic_mixer_bus`.
  - **BLINK strip:** shown only while Blink runs (CuteMusic tests the lock on `~/.blink/lock` every 2 s). It plays
    `cutemusic_mixer_in.monitor` on CuteMusic's output and follows the output knob.
  - **MUSIC strip:** VOCAL CUT, **TO CALLS** (the music into `cutemusic_mixer_bus` too) and the volume fader.
- **The ALSA device:** `alsa/60-ag-cute-music.conf`, installed by the package as
  `/etc/alsa/conf.d/60-ag-cute-music.conf`, else set up by CuteMusic in the user's ALSA configuration (see
  Requirements):

  ```
  pcm.ag_cutemusic_mixer {
      type pipewire
      playback_node "cutemusic_mixer_in"
      capture_node "cutemusic_mixer_out"
      hint { show on; description "AG Podcast Mixer" }
  }
  ```

  Blink's SIP engine (pjmedia's ALSA backend) opens each ALSA device while listing it (so the plugin must be
  installed: a definition `aplay -L` shows can still be left out) and lists it by its hint description, so the device appears as
  "AG Podcast Mixer", on both the input and the output side. Blink's Devices menu therefore shows it as one combined
  device, like a USB headset.

## Blink side

`blink/cutemusic.py`:

- **`visible()`:** device lists leave out "AG Podcast Mixer" unless CuteMusic holds its lock. It is used for the
  Devices menu (input, output, alert, and the combined devices at the top), for Preferences → Audio, and for
  `MainWindow.refresh_devices()`.
- **`Watcher`:** tests the lock every 2 s. When CuteMusic starts or quits, the device menus are rebuilt. If CuteMusic
  quits while its mixer is chosen, `refresh_devices()` puts the input and output back to System default, as for an
  unplugged headset. Choosing the mixer again after CuteMusic restarts is left to the user.
- **`apply_music_mode()`:** runs when the input device changes.
  - The mixer becomes the input: Blink notes the current echo-cancelling and sample-rate settings (in
    `<data>/cutemusic.json`), then sets echo cancelling off and 48 kHz.
  - Another input is chosen: those settings are put back.
  - Preferences show what is in effect. The SIP path and the WebRTC path both follow these settings.

`blink/webrtc/videoroom.py`:

- **Echo cancelling and bitrate** follow the setting. On: mono, `webrtcdsp`, Opus at 64 kbit/s, as before. Off: stereo,
  no `webrtcdsp`, Opus at 128 kbit/s.
- **Devices:** with the mixer chosen and CuteMusic running, the room records from `pulsesrc
  device=cutemusic_mixer_out` and plays to `pulsesink device=cutemusic_mixer_in`. Otherwise it uses `autoaudiosrc` and
  `autoaudiosink` as before. The room is set up when it is joined; switching devices mid-room takes effect at the next
  join.

## Setting up a session

1. Start AG CuteMusic and Blink, and put on a headset. With echo cancelling off, speakers would let the friends hear
   themselves through the mic.
2. In CuteMusic, open View → Mixer: choose the headset microphone in INPUT, and turn TO CALLS on to send the music.
3. In Blink, open Devices → AG Podcast Mixer.
4. Call, or join the room. Balance the MIC, BLINK and MUSIC faders; the MUSIC fader is also the local volume.
5. Afterwards, choose the headset (or System default) in Blink again: echo cancelling and the sample rate come back.

## Requirements

- PulseAudio, or PipeWire with pipewire-pulse.
- `pactl` from pulseaudio-utils.
- PipeWire's ALSA plugin (`pipewire-alsa`). Without it, CuteMusic's own set-up falls back to PulseAudio's (`libasound2-plugins`).
- GStreamer `pulsesrc` and `pulsesink`, `audiokaraoke`, `audioecho` and `level` (gst-plugins-good).
- The ALSA device. The package installs `alsa/60-ag-cute-music.conf` in `/etc/alsa/conf.d`. Without it (run from
  the source tree, no root), CuteMusic sets the device up itself at its first start: it writes the definition to
  `~/.config/alsa/ag-cute-music.conf` and includes it from `~/.config/alsa/asoundrc`. Blink reads ALSA's configuration
  when it starts, so a Blink already running sees the device after a restart; CuteMusic says so when that is the case.

## Known limits and open points

- **Latency, for jamming:** the round trip (network, jitter buffer, 20 ms Opus frames, the sound server's buffers)
  is usually well over the 25–30 ms musicians can play tightly against. It suits a soloist over a groove or
  taking turns; playing in lockstep needs a low-latency path: smaller Opus frames, a smaller jitter buffer, peers
  on the same network.
- **Other ALSA programs:** ones that list devices themselves (Audacity, say) show "AG Podcast Mixer" all the time.
  Opening it while CuteMusic is not running fails, as there are no devices behind it.
- **Preferences open while CuteMusic starts or quits:** the dialog's device lists are not refreshed until it is
  opened again.
- **CuteMusic gone mid-call:** a SIP call keeps the ALSA device and gets silence until the next call. A room keeps its
  `pulsesrc`/`pulsesink`, which the sound server may move to the defaults.
- **Single music level:** the music level sent is the local volume. There is no separate send level.
- **Alerts:** the alert device can be set to the mixer too, so that ringing comes through the BLINK strip.
