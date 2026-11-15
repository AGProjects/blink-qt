"""AG Podcast Mixer in WebRTC rooms: the room's audio through the link's rings (blink/agpm.py,
docs/podcast-mixer-link-migration.md), as the SDK's "AG Podcast Mixer" device does for SIP calls.

    to-call.ring   -> MixerSource (appsrc) -> the room's microphone chain     (AG CuteMusic's mix: the karaoke)
    the room's mix -> MixerSink (appsink)  -> from-call.ring                   (AG CuteMusic's BLINK strip)

48 kHz stereo s16le on both sides, the link's format.

MixerSource: AG CuteMusic writes to-call at its own pace, in its own chunks. A thread reads it every 10 ms and
pushes what the clock says is due, so the room gets a continuous stream whatever AG CuteMusic does: it waits
until PRIME is there before taking from the ring (a cushion for AG CuteMusic's jitter), fills a shortfall with
silence and primes again (an underrun), and drops back to PRIME when more than MAX piles up (AG CuteMusic's clock
ahead of ours, or a stall here). AG CuteMusic quitting or restarting mid-room is silence, then its sound again (the
ring's generation).

MixerSink: the room's mix, at the pipeline's clock (sync), written to from-call. The writer never waits: what does
not fit (AG CuteMusic not reading) is dropped and counted.

The rings are one writer, one reader: a SIP call on the SDK's device at the same time as a room on the mixer would
read to-call and write from-call too. Not guarded here.
"""

import threading
import time

from blink import agpm


__all__ = ['open_link', 'MixerSource', 'MixerSink', 'CAPS']


CAPS = f'audio/x-raw,format=S16LE,layout=interleaved,rate={agpm.RATE},channels={agpm.CHANNELS}'

TICK = 0.010                # s
PRIME = 960                 # frames: 20 ms taken from the ring before its sound is used
MAX = 2880                  # frames: 60 ms; more than that piled up is dropped back to PRIME


class MixerSource(object):
    """to-call.ring as an appsrc (element), fed by a thread between start() and stop()."""

    def __init__(self, Gst, make):
        self._Gst = Gst
        self.ring = agpm.Ring('to-call.ring')      # OSError, ValueError: no rings (AG CuteMusic never ran with the link)
        self.element = make('appsrc', 'microphone', is_live=True, do_timestamp=True, format='time', block=False,
                            caps=Gst.Caps.from_string(CAPS), max_bytes=agpm.RATE * agpm.FRAME_BYTES // 5, leaky_type='downstream')
        self.underruns = 0
        self.drops = 0
        self._running = False
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name='AGPM to-call', daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(1)
            self._thread = None
        self.ring.close()

    def _push(self, data):
        Gst = self._Gst
        self.element.emit('push-buffer', Gst.Buffer.new_wrapped(data))

    def _run(self):
        ring = self.ring
        ring.reader_sync()
        silence = bytes(agpm.FRAME_BYTES * agpm.RATE // 10)     # 100 ms, more than a tick ever needs
        priming = True
        start = time.monotonic()
        pushed = 0
        while self._running:
            time.sleep(TICK)
            due = int((time.monotonic() - start) * agpm.RATE) - pushed
            if due <= 0:
                continue
            if due > agpm.RATE // 10:       # a stall here (suspend, a long GC): start the clock again
                start, pushed, due = time.monotonic(), 0, agpm.QUANTUM
            available = ring.available()
            if priming:
                if available >= PRIME:
                    priming = False
                else:
                    self._push(silence[:due * agpm.FRAME_BYTES])
                    pushed += due
                    continue
            if available > MAX:
                ring.read(available - PRIME)        # dropped
                available = PRIME
                self.drops += 1
            data = ring.read(due)
            if len(data) < due * agpm.FRAME_BYTES:
                data += silence[:due * agpm.FRAME_BYTES - len(data)]
                self.underruns += 1
                priming = True
            self._push(data)
            pushed += due


class MixerSink(object):
    """from-call.ring as an appsink (element): what the room plays, written there."""

    def __init__(self, Gst, make):
        self._Gst = Gst
        self.ring = agpm.Ring('from-call.ring')
        self.element = make('appsink', 'speakers', emit_signals=True, sync=True, max_buffers=4, drop=True,
                            caps=Gst.Caps.from_string(CAPS))
        self.element.connect('new-sample', self._on_sample)
        self.dropped_frames = 0
        self._open = True
        self.ring.writer_attach()       # AG CuteMusic's BLINK strip sees a Blink writing from-call

    def _on_sample(self, sink):
        # streaming thread
        Gst = self._Gst
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.EOS
        if not self._open:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = bytes(info.data)
            finally:
                buffer.unmap(info)
            if not self.ring.write(data):
                self.dropped_frames += len(data) // agpm.FRAME_BYTES
        return Gst.FlowReturn.OK

    def stop(self):
        """After the pipeline is stopped: no sample comes any more."""
        if self._open:
            self._open = False
            self.ring.close()


def open_link(Gst, make):
    """(MixerSource, MixerSink), or raise OSError / ValueError when the rings are not there or not valid."""
    source = MixerSource(Gst, make)
    try:
        sink = MixerSink(Gst, make)
    except Exception:
        source.ring.close()
        raise
    return source, sink
