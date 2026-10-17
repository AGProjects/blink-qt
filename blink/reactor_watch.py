"""Says so when the SIP SIMPLE SDK's reactor thread is blocked, and where.

DNS lookups (green sockets), XCAP, SIP timers and the SDK's notifications all run in the
twisted reactor thread. When code running there blocks (a synchronous network call, a
long computation, a lock), every one of them stalls at once: DNS lookups time out while
the system resolver answers other programs at once. An OS thread schedules a call into
the reactor every second; when the call has not run after `threshold` seconds, the stack
of the reactor thread is logged, so the code holding it is named. When the call finally
runs, how long the reactor was blocked is logged too.
"""

import sys
import threading
import time
import traceback

from blink.logging import ActivityLog


__all__ = ['ReactorWatch']


class ReactorWatch(object):

    interval = 1.0          # seconds between pings
    threshold = 2.0         # seconds a ping may wait before the reactor counts as blocked
    stack_depth = 20        # frames of the reactor thread logged

    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self._reactor_thread_id = None

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='Blink reactor watch', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None

    def _run(self):
        from twisted.internet import reactor
        while not self._stop.wait(self.interval):
            done = threading.Event()
            sent = time.monotonic()

            def ping():
                self._reactor_thread_id = threading.get_ident()
                done.set()

            try:
                reactor.callFromThread(ping)
            except Exception:
                return          # the reactor is gone: Blink is stopping
            if done.wait(self.threshold):
                continue
            self._log_stack(time.monotonic() - sent)
            while not done.wait(1.0):
                if self._stop.is_set():
                    return
            blocked = time.monotonic() - sent
            ActivityLog().warning(f'[reactor] The SIP reactor thread was blocked for {blocked:.1f} seconds (DNS, XCAP and SIP timers waited that long)')

    def _log_stack(self, waited):
        thread_id = self._reactor_thread_id
        frame = sys._current_frames().get(thread_id) if thread_id is not None else None
        if frame is None:
            ActivityLog().warning(f'[reactor] The SIP reactor thread has not run a scheduled call for {waited:.1f} seconds (its stack is not known yet)')
            return
        stack = ''.join(traceback.format_stack(frame, limit=self.stack_depth)).rstrip()
        ActivityLog().warning(f'[reactor] The SIP reactor thread is blocked ({waited:.1f} seconds without running a scheduled call), it is at:\n{stack}')
