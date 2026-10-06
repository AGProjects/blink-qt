# Copyright (C) 2026 AG Projects. See LICENSE for details.
#
"""application/sylk-addressbook-update -- "the addressbook changed, refetch it".

After WE write to the server addressbook, one message to our OWN account tells
every other device to fetch the new document; when another device tells US, we
fetch. The message carries no contact data: XCAP is the source of truth and
this only says where to look. It is never journalled (X-Sylk-Skip-Journal),
never stored, never rendered.

The wire format and the timing rules are specified in the sylk-mobile tree,
docs/messages/sylk-addressbook-update.md; both clients implement the same
constants under the same names.

Nothing here touches Qt, the notification centre or the network: it is the
wire format plus the two state machines that answer "may this happen yet".
blink.contacts.AddressbookNotifier owns the timers and does the sending and
fetching. That split is what makes a five-minute fuse something a test can
reach. Ported verbatim from Blink for macOS (AddressbookNotify.py).
"""

import json
import random
import time

__all__ = ['CONTENT_TYPE', 'SKIP_JOURNAL_HEADER', 'build_tick', 'parse_tick',
           'is_fresh', 'jitter_delay', 'SendThrottle', 'FetchScheduler']


CONTENT_TYPE = 'application/sylk-addressbook-update'
SKIP_JOURNAL_HEADER = 'X-Sylk-Skip-Journal'
PAYLOAD_VERSION = 1

# --- send side -------------------------------------------------------------
# One tick per burst, never one per contact.
NOTIFY_DEBOUNCE = 2.0           # quiet time after the last write
NOTIFY_MAX_DEFER = 30.0         # ... but never defer a burst longer than this
NOTIFY_MIN_INTERVAL = 10.0      # floor between two ticks
NOTIFY_FUSE_MAX = 6             # ticks per window before we stop sending
NOTIFY_FUSE_WINDOW = 300.0
ID_LIST_CAP = 64                # past this the lists are dropped, truncated set

# --- receive side ----------------------------------------------------------
# Every device on the account gets the same tick in the same instant, so a
# fetch is always jittered: without it one edit here becomes a synchronised GET
# from every device the account owns.
FETCH_JITTER_MIN = 2.0
FETCH_JITTER_MAX = 9.0
FETCH_MIN_INTERVAL = 15.0       # never below FETCH_JITTER_MAX
FETCH_FUSE_MAX = 10             # fetches per window before backing off
FETCH_FUSE_WINDOW = 300.0
FETCH_BACKOFF = (30.0, 60.0, 120.0, 300.0)
FRESHNESS_WINDOW = 120          # seconds; judged ON ARRIVAL only

# Only the addressbook document. pres-rules and dialog-rules are out of scope:
# they never trigger a tick and a tick never refetches them.
FETCH_DOCUMENTS = frozenset(['resource-lists'])


def _clean_ids(ids):
    out = []
    for value in ids or ():
        if value is None:
            continue
        text = str(value)
        if text and text not in out:
            out.append(text)
    return out


def build_tick(origin, contact_ids=(), group_ids=(), truncated=False, timestamp=None):
    """The JSON body of a tick.

    Present-vs-absent is load-bearing: an empty list means "nothing in that
    collection changed", an ABSENT list means "unknown, assume everything did".
    A truncated tick therefore omits both lists rather than sending an empty
    pair, which would claim nothing had changed.
    """
    body = {'v': PAYLOAD_VERSION,
            'origin': str(origin or ''),
            'timestamp': int(timestamp if timestamp is not None else time.time())}
    if truncated:
        body['truncated'] = True
    else:
        body['contactIds'] = _clean_ids(contact_ids)
        body['groupIds'] = _clean_ids(group_ids)
    return json.dumps(body)


def parse_tick(content):
    """Parse a tick body, or return None for anything that is not a v1 tick."""
    if isinstance(content, bytes):
        try:
            content = content.decode()
        except UnicodeDecodeError:
            return None
    try:
        body = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get('v') != PAYLOAD_VERSION:
        return None
    timestamp = body.get('timestamp')
    if not isinstance(timestamp, int):
        return None
    contact_ids = body.get('contactIds')
    group_ids = body.get('groupIds')
    return {'v': PAYLOAD_VERSION,
            'origin': str(body.get('origin') or ''),
            'timestamp': timestamp,
            # None (not []) when absent: "unknown, assume everything changed".
            'contact_ids': _clean_ids(contact_ids) if isinstance(contact_ids, list) else None,
            'group_ids': _clean_ids(group_ids) if isinstance(group_ids, list) else None,
            'truncated': bool(body.get('truncated'))}


def is_fresh(timestamp, now=None, window=FRESHNESS_WINDOW):
    """Judged ON ARRIVAL, before any timer is armed -- never again when the
    jitter expires, or a 30s delay on a 110s-old tick would throw away a fetch
    that is still wanted."""
    if not isinstance(timestamp, int):
        return False
    age = (time.time() if now is None else now) - timestamp
    return -10 <= age <= window       # 10 seconds of clock skew


def jitter_delay(rand=None):
    r = random.random() if rand is None else rand()
    return FETCH_JITTER_MIN + r * (FETCH_JITTER_MAX - FETCH_JITTER_MIN)


def _prune(stamps, now, window):
    while stamps and now - stamps[0] > window:
        stamps.pop(0)
    return stamps


class SendThrottle(object):
    """Accumulates a burst and answers: may we send yet, and with what?

    The debounce collapses a burst into one tick. The floor and the fuse are
    the second line of defence, against a BUG -- an adopt path we missed, two
    devices answering each other. Six addressbook bursts in five minutes is not
    someone editing contacts, it is a loop, and the fuse tripping is a bug
    report rather than a number to tune.
    """

    def __init__(self, now=None):
        self._now = now or time.time
        self._contacts = set()
        self._groups = set()
        self._truncated = False
        self._first_pending = None
        self._last_write = None
        self._last_send = None
        self._sends = []
        self._suppress = 0

    def suppress(self):
        """Enter a stretch whose writes must not be announced -- anything we
        are echoing back from the server. Reentrant."""
        self._suppress += 1

    def resume(self):
        if self._suppress > 0:
            self._suppress -= 1

    @property
    def suppressed(self):
        return self._suppress > 0

    def note(self, kind, id):
        """Record one successful write. Returns False when suppressed."""
        if self._suppress > 0:
            return False
        now = self._now()
        if self._first_pending is None:
            self._first_pending = now
        self._last_write = now
        if not id:
            # An op that cannot name its id makes the whole tick truncated:
            # better to say "assume everything" than to send a list that
            # silently omits a row.
            self._truncated = True
        elif kind == 'group':
            self._groups.add(str(id))
        else:
            self._contacts.add(str(id))
        if len(self._contacts) > ID_LIST_CAP or len(self._groups) > ID_LIST_CAP:
            self._truncated = True
        return True

    @property
    def pending(self):
        return self._first_pending is not None

    def delay(self):
        """Seconds until the caller should look again; None when idle.

        The debounce is re-armed by every write; MAX_DEFER puts a ceiling on
        that, so a slow drip cannot defer the tick for ever.
        """
        if not self.pending:
            return None
        now = self._now()
        due = min(self._last_write + NOTIFY_DEBOUNCE,
                  self._first_pending + NOTIFY_MAX_DEFER)
        floor = 0 if self._last_send is None else self._last_send + NOTIFY_MIN_INTERVAL
        return max(0.0, max(due, floor) - now)

    def take(self, settled=True):
        """Drain the burst if it is due AND settled, else None.

        `settled` is the caller's answer to "is anything of ours still in
        flight": a tick sent before our own writes have reached the server
        sends the other devices to fetch a document we are still writing.
        """
        if not self.pending or self.delay() > 0 or settled is False:
            return None
        now = self._now()
        _prune(self._sends, now, NOTIFY_FUSE_WINDOW)
        if len(self._sends) >= NOTIFY_FUSE_MAX:
            return None                      # ids are kept; they go out when the window drains
        # Still being written to? Then this is a MAX_DEFER flush and the lists
        # do not describe a finished burst.
        still_writing = now < self._last_write + NOTIFY_DEBOUNCE
        truncated = (self._truncated or still_writing
                     or len(self._contacts) > ID_LIST_CAP
                     or len(self._groups) > ID_LIST_CAP)
        tick = (sorted(self._contacts), sorted(self._groups), truncated)
        self._contacts = set()
        self._groups = set()
        self._truncated = False
        self._first_pending = None
        self._last_write = None
        self._last_send = now
        self._sends.append(now)
        return tick

    @property
    def fuse_blown(self):
        _prune(self._sends, self._now(), NOTIFY_FUSE_WINDOW)
        return len(self._sends) >= NOTIFY_FUSE_MAX


class FetchScheduler(object):
    """Decides when an arriving tick turns into a fetch.

    Never immediately: every device received the same tick at the same instant.
    Never while our own writes are settling: a document landing on top of our
    own unflushed journal is how an update ends up re-pushing rows we were in
    the middle of replacing.
    """

    def __init__(self, now=None, rand=None):
        self._now = now or time.time
        self._rand = rand
        self._armed = False
        self._in_flight = False
        self._pending_after_flight = False
        self._last_fetch = None
        self._fetches = []
        self._backoff_step = 0
        self._backoff_until = 0.0

    @property
    def armed(self):
        return self._armed

    def schedule(self):
        """A tick arrived. Returns the delay to arm a fetch at, or None when it
        merged into a fetch that is already coming (the caller unions the ids
        into that one instead of arming a second)."""
        if self._armed:
            return None
        if self._in_flight:
            self._pending_after_flight = True
            return None
        now = self._now()
        delay = jitter_delay(self._rand)
        if self._last_fetch is not None:
            delay = max(delay, self._last_fetch + FETCH_MIN_INTERVAL - now)
        if self._backoff_until > now:
            delay = max(delay, self._backoff_until - now)
        self._armed = True
        return max(0.0, delay)

    def fire(self, settled=True):
        """The armed timer fired. Returns (fetch, retry_in, backed_off)."""
        self._armed = False
        if settled is False:
            return False, self.schedule(), False
        now = self._now()
        _prune(self._fetches, now, FETCH_FUSE_WINDOW)
        if len(self._fetches) >= FETCH_FUSE_MAX:
            # Something upstream is looping. Protect the server even though the
            # sender's own fuse should have caught this first.
            step = min(self._backoff_step, len(FETCH_BACKOFF) - 1)
            self._backoff_until = now + FETCH_BACKOFF[step]
            self._backoff_step += 1
            return False, self.schedule(), True
        self._fetches.append(now)
        self._last_fetch = now
        self._in_flight = True
        return True, None, False

    def done(self):
        """The fetch finished -- either way; a failed one still used a slot."""
        self._in_flight = False
        if self._pending_after_flight:
            self._pending_after_flight = False
            return self.schedule()
        return None

    def reset(self):
        self._backoff_step = 0
        self._backoff_until = 0.0
        del self._fetches[:]
