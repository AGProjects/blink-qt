"""AG Podcast Mixer link, Python side (python3-sipsimple docs/PodcastMixerLink.md).

AG CuteMusic's mixer and Blink exchange 48 kHz stereo s16le PCM through two shared-memory rings in a per-user
directory. The SDK (pjmedia-audiodev/agpm_dev.c) turns them into the "AG Podcast Mixer" audio device for SIP
calls; this module is for the Python ends: AG CuteMusic (writes to-call, reads from-call) and Blink Qt's WebRTC
rooms (read to-call, write from-call).

The canonical copy is python3-sipsimple/tools/agpm.py; AG CuteMusic and Blink Qt carry copies of it. Keep them
the same, and in step with agpm.h.

- mixer.lock: AG CuteMusic holds an exclusive flock while its mixer runs (MixerLock).
- blink.lock: Blink holds it while it runs (ProcessLock); AG CuteMusic shows its BLINK strip then.
- to-call.ring, from-call.ring: created by AG CuteMusic (create_rings), opened by Blink (Ring).
- calls.json: who is in the call (written by Blink, read by AG CuteMusic).

Ring: a 64-byte header (agpm.h, pjmedia_agpm_ring_header), then capacity frames of 2 x s16le. One writer, one
reader; the writer never waits (it drops what does not fit), the reader resyncs when the writer restarts
(generation) or falls a whole ring behind.
"""

import fcntl
import mmap
import os
import struct
import sys

__all__ = ['DEVICE', 'RATE', 'CHANNELS', 'QUANTUM', 'FRAME_BYTES', 'link_dir', 'path', 'mixer_running', 'blink_running',
           'MixerLock', 'ProcessLock', 'Ring', 'create_rings']


DEVICE = 'AG Podcast Mixer'
MAGIC = 0x4d504741          # "AGPM"
VERSION = 1
RATE = 48000
CHANNELS = 2
QUANTUM = 240               # frames: 5 ms
FRAME_BYTES = CHANNELS * 2
HEADER_SIZE = 64
CAPACITY = 4096             # frames: 85 ms

# offsets in the header
_MAGIC, _VERSION, _CHANNELS, _RATE, _CAPACITY, _PID, _GENERATION, _WRITE, _READ = 0, 4, 6, 8, 12, 16, 20, 24, 32


def link_dir():
    """$AGPM_DIR, else $XDG_RUNTIME_DIR/ag-podcast-mixer (Linux), $TMPDIR/ag-podcast-mixer (macOS), else
    /tmp/ag-podcast-mixer-<uid>: the same rule as pjmedia_agpm_dir()."""
    base = os.environ.get('AGPM_DIR')
    if base:
        return base
    if sys.platform == 'darwin':
        base = os.environ.get('TMPDIR')
        if base:
            return os.path.join(base, 'ag-podcast-mixer')
    else:
        base = os.environ.get('XDG_RUNTIME_DIR')
        if base:
            return os.path.join(base, 'ag-podcast-mixer')
    return '/tmp/ag-podcast-mixer-%d' % os.getuid()


def path(name):
    return os.path.join(link_dir(), name)


def _held(name):
    """Whether another process holds an exclusive flock on name (a shared lock cannot be taken then)."""
    try:
        with open(path(name), 'rb') as file:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def mixer_running():
    """Whether AG CuteMusic's mixer runs (it holds mixer.lock)."""
    return _held('mixer.lock')


def blink_running():
    """Whether Blink runs (it holds blink.lock)."""
    return _held('blink.lock')


class ProcessLock(object):
    """An exclusive flock on one of the link's lock files, held while this process wants to be seen; the lock
    goes with the process, so a crash leaves nothing behind."""

    def __init__(self, name):
        self.name = name
        self._file = None

    def acquire(self):
        if self._file is not None:
            return True
        os.makedirs(link_dir(), mode=0o700, exist_ok=True)
        file = open(path(self.name), 'ab')
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            file.close()
            return False        # another instance holds it
        self._file = file
        return True

    def release(self):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    @property
    def held(self):
        return self._file is not None


class MixerLock(ProcessLock):
    """mixer.lock: held by AG CuteMusic while its mixer runs (the SDK lists the device then)."""

    def __init__(self):
        super().__init__('mixer.lock')


def _valid(mm, size):
    if size < HEADER_SIZE:
        return False
    magic, version, channels, rate, capacity = struct.unpack_from('<IHHII', mm, 0)
    return (magic == MAGIC and version == VERSION and channels == CHANNELS and rate == RATE and capacity > 0 and
            capacity & (capacity - 1) == 0 and size >= HEADER_SIZE + capacity * FRAME_BYTES)


def create_rings(capacity=CAPACITY):
    """AG CuteMusic: to-call.ring and from-call.ring, made if missing or not valid. A valid ring is kept as it is
    (never unlinked: a Blink that mapped it keeps working)."""
    os.makedirs(link_dir(), mode=0o700, exist_ok=True)
    os.chmod(link_dir(), 0o700)
    for name in ('to-call.ring', 'from-call.ring'):
        fd = os.open(path(name), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size >= HEADER_SIZE:
                mm = mmap.mmap(fd, size)
                valid = _valid(mm, size)
                mm.close()
                if valid:
                    continue
            size = HEADER_SIZE + capacity * FRAME_BYTES
            os.ftruncate(fd, 0)
            os.ftruncate(fd, size)
            mm = mmap.mmap(fd, size)
            struct.pack_into('<IHHII', mm, 0, MAGIC, VERSION, CHANNELS, RATE, capacity)
            mm.close()
        finally:
            os.close(fd)


class Ring(object):
    """One ring, opened as its writer or its reader. Not thread-safe: one thread per end."""

    def __init__(self, name):
        self.name = name
        fd = os.open(path(name), os.O_RDWR)
        try:
            size = os.fstat(fd).st_size
            self._mm = mmap.mmap(fd, size)
        finally:
            os.close(fd)
        if not _valid(self._mm, size):
            self._mm.close()
            raise ValueError('%s is not a valid AG Podcast Mixer ring' % path(name))
        self.capacity = struct.unpack_from('<I', self._mm, _CAPACITY)[0]
        self._generation = self._load(_GENERATION, '<I')

    def close(self):
        self._mm.close()

    def _load(self, offset, fmt='<Q'):
        return struct.unpack_from(fmt, self._mm, offset)[0]

    def _store(self, offset, value, fmt='<Q'):
        struct.pack_into(fmt, self._mm, offset, value)

    # As the writer

    def writer_attach(self):
        """Start writing: empty for the reader, which resyncs on the new generation."""
        self._store(_PID, os.getpid(), '<I')
        self._store(_WRITE, self._load(_READ))
        self._store(_GENERATION, (self._load(_GENERATION, '<I') + 1) & 0xffffffff, '<I')

    def write(self, data):
        """Frames (interleaved stereo s16le bytes); False, and nothing written, when they do not fit."""
        frames = len(data) // FRAME_BYTES
        w, r = self._load(_WRITE), self._load(_READ)
        if w - r + frames > self.capacity:
            return False
        start = w % self.capacity
        first = min(frames, self.capacity - start)
        offset = HEADER_SIZE + start * FRAME_BYTES
        self._mm[offset:offset + first * FRAME_BYTES] = data[:first * FRAME_BYTES]
        if first < frames:
            self._mm[HEADER_SIZE:HEADER_SIZE + (frames - first) * FRAME_BYTES] = data[first * FRAME_BYTES:frames * FRAME_BYTES]
        self._store(_WRITE, w + frames)      # after the data: the reader takes the index as the sign
        return True

    # As the reader

    def reader_sync(self):
        """From now on only what is written from now on."""
        self._generation = self._load(_GENERATION, '<I')
        self._store(_READ, self._load(_WRITE))

    def available(self):
        if self._load(_GENERATION, '<I') != self._generation:
            self.reader_sync()
        w, r = self._load(_WRITE), self._load(_READ)
        if w < r or w - r > self.capacity:
            self.reader_sync()
            return 0
        return w - r

    def read(self, frames):
        """Up to frames frames (bytes); fewer when fewer are there."""
        frames = min(frames, self.available())
        r = self._load(_READ)
        start = r % self.capacity
        first = min(frames, self.capacity - start)
        offset = HEADER_SIZE + start * FRAME_BYTES
        data = self._mm[offset:offset + first * FRAME_BYTES]
        if first < frames:
            data += self._mm[HEADER_SIZE:HEADER_SIZE + (frames - first) * FRAME_BYTES]
        self._store(_READ, r + frames)
        return data
