# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""BackgroundDecryptor: opens, slowly, the stored messages that still hold PGP ciphertext.

Messages from the journal are stored encrypted unless their conversation was
open when they arrived, and messages that came before the account's key are
stored encrypted too. The message pane decrypts what comes into view
(ConversationModel.decrypt_rows); this goes through the rest in the
background, newest first, so search, filters and previews see plaintext.

It must not be felt in the UI, so:
- the decryption runs in a thread of its own ('pgp-decrypt'), never in the GUI
  thread nor in the 'db' thread the UI reads history through;
- the database is only touched in short steps in the 'db' thread: the ids to
  do, once per run (no content), then one batch of contents, then the results;
- it pauses between messages and between batches, and backs off while the
  message pane is decrypting what is in view.

Per account with a private key here: rows with decrypted '0' (never tried) or
'3' (to try again after a key change) whose content is ciphertext. A message
that opens is stored as plaintext (MessageHistory.store_decrypted_message); one
that does not is marked '2' with the reason (store_undecryptable_message), as a
live message that failed, so it is not tried again until the key changes.
Rows changed are announced as BlinkMessagesDidDecryptInBackground (data.ids),
for the open conversation to update them in place.

A run starts 30 seconds after Blink starts, after each journal sync, and when
an account's private key changes; a run asked for while one is going starts
again when it ends.
"""

__all__ = ['BackgroundDecryptor']

import os
import time
import warnings

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.threading import run_in_thread

from blink.logging import ActivityLog
from blink.uris import BONJOUR_ACCOUNT_ID
from blink.util import call_in_gui_thread, call_later, run_in_gui_thread


# The worker: a plain python3 process that imports pgpy only (not blink, not Qt), at the lowest
# priority, so the desktop and Blink's UI always come first. JSON lines on stdin and stdout:
#   {"account": id, "key": armored private key}   remembered, no answer
#   {"duty": 0.1..1}                               share of a core it may use, no answer
#   {"id": message id, "account": id, "content": ciphertext}
#       -> {"id": ..., "plaintext": ...} or {"id": ..., "error": ...}
WORKER_CODE = r"""
import json, os, sys, time, warnings
warnings.simplefilter('ignore')
try:
    os.nice(19)
except OSError:
    pass
import pgpy
keys, duty = {}, 1.0
for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    if 'key' in request:
        try:
            keys[request['account']] = pgpy.PGPKey.from_blob(request['key'])[0]
        except Exception as e:
            keys[request['account']] = e
        continue
    if 'duty' in request:
        duty = min(1.0, max(0.02, float(request['duty'])))
        continue
    began = time.thread_time()
    answer = {'id': request.get('id')}
    key = keys.get(request.get('account'))
    try:
        if key is None or isinstance(key, Exception):
            raise RuntimeError(f'no usable private key: {key}')
        plaintext = key.decrypt(pgpy.PGPMessage.from_blob(request['content'])).message
        if isinstance(plaintext, (bytes, bytearray)):
            plaintext = bytes(plaintext).decode('utf-8')
        answer['plaintext'] = plaintext
    except Exception as e:
        answer['error'] = str(e) or e.__class__.__name__
    sys.stdout.write(json.dumps(answer) + '\n')
    sys.stdout.flush()
    if duty < 1.0:
        time.sleep((time.thread_time() - began) * (1 / duty - 1))
"""


class DecryptWorker(object):
    """One worker process, used from the 'pgp-decrypt' thread only. Its answers are read by a
    thread of its own into a queue (select on a buffered pipe would miss lines already read)."""

    answer_timeout = 60.0       # seconds for one message: longer, and the worker is taken for hung

    def __init__(self):
        import queue
        import subprocess
        import sys
        import threading
        self.process = subprocess.Popen([sys.executable, '-I', '-c', WORKER_CODE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1)
        self.answers = queue.Queue()
        self.keys = {}          # account id: the key file mtime sent
        self.duty = None
        threading.Thread(target=self._read, name='pgp-decrypt-worker-reader', daemon=True).start()

    def _read(self):
        import json
        try:
            for line in self.process.stdout:
                try:
                    self.answers.put(json.loads(line))
                except ValueError:
                    pass
        except (OSError, ValueError):
            pass
        self.answers.put(None)      # it ended

    @property
    def alive(self):
        return self.process.poll() is None

    def send(self, request):
        import json
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()

    def receive(self):
        """The next answer, or None (died, or hung: then it is stopped)."""
        import queue
        try:
            answer = self.answers.get(timeout=self.answer_timeout)
        except queue.Empty:
            self.stop()
            return None
        if answer is None:
            self.answers.put(None)  # stays ended for any later receive
        return answer

    def stop(self):
        try:
            self.process.kill()
            self.process.wait(timeout=5)
        except Exception:
            pass


@implementer(IObserver)
class BackgroundDecryptor(object, metaclass=Singleton):
    start_delay = 30.0          # seconds after Blink starts
    batch_size = 20
    # The speed, chosen by the user (the button by the progress bar): at most this share of one
    # CPU -- after a message that took t of CPU, sleep t * (1 / duty - 1) -- while Blink is the
    # application in use and while it is not, and the seconds between two batches.
    speeds = {
        'fast':   (0.8, 0.9, 0.2),
        'normal': (0.15, 0.5, 1.0),
        'gentle': (0.05, 0.2, 3.0),
    }
    # With worker processes (blink.pgp_decryptor.WORKER_CODE, at the lowest priority), the number
    # of them and the share of a core each may use: Fast takes every core but one.
    @staticmethod
    def _workers_for(speed):
        cores = os.cpu_count() or 2
        return {'fast': (max(1, cores - 1), 1.0), 'normal': (min(2, max(1, cores - 1)), 0.5), 'gentle': (1, 0.15)}.get(speed, (1, 0.15))
    speed_names = (('fast', 'Fast'), ('normal', 'Normal'), ('gentle', 'Gentle'), ('paused', 'Pause'))     # as in the menu
    speed_descriptions = {
        'fast': 'Uses most of one CPU core, finishes soonest',
        'normal': 'Light while you use Blink, faster when it is in the background',
        'gentle': 'Hardly noticeable, takes longest',
        'paused': 'Stopped until resumed',
    }
    speed_file = 'pgp_decryption_speed'     # <data>/pgp_decryption_speed: the choice, kept between runs
    message_pause = 0.02        # and never less than this, in seconds, between two messages
    busy_pause = 5.0            # seconds to wait while the message pane decrypts what is in view
    progress_every = 200        # messages between two progress lines in the Activity log

    def __init__(self):
        self._started = False
        self.running = False
        self._again = False
        self._queue = []            # [(account id, message id, timestamp)] left in this run, newest first
        self._reached = None        # the time of the oldest message of the last batch (the progress line)
        self._failed = set()        # message ids that would not open in this run
        self._keys = {}             # account id: (path, mtime, PGPKey or None), used in the decrypting thread only
        self._done = self._opened = self._not_opened = 0
        self._total = 0             # messages found at the start of the run (the progress bar)
        self._started_at = 0.0
        self._mark = (0.0, 0)
        self.eta = None
        self.total_opened = self.total_not_opened = 0      # since Blink started, over all runs (logged at quit)
        self.last_view_decrypt = 0.0    # time.time() of the pane's last decrypt_rows
        self.speed = self._load_speed()
        self._held = False          # a batch was due while paused: started on resume
        self._workers = []          # DecryptWorker, in the 'pgp-decrypt' thread
        self.use_workers = True     # False once workers could not be started: decrypted in-process, as before

    def start(self):
        if self._started:
            return
        self._started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkJournalDidApply')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        call_in_gui_thread(call_later, self.start_delay, self.run, 'start')     # a QTimer: armed in the GUI thread
        call_in_gui_thread(self._follow_application_state)

    in_use = True       # Blink is the active application (written in the GUI thread, read in the decrypting one)

    def _follow_application_state(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QGuiApplication
        application = QGuiApplication.instance()
        if application is None:
            return

        def changed(state):
            self.in_use = state == Qt.ApplicationState.ApplicationActive
        application.applicationStateChanged.connect(changed)
        changed(application.applicationState())

    @property
    def duty_cycle(self):
        active, idle, _pause = self.speeds.get(self.speed, self.speeds['normal'])
        return active if self.in_use else idle

    @property
    def batch_pause(self):
        return self.speeds.get(self.speed, self.speeds['normal'])[2]

    @property
    def paused(self):
        return self.speed == 'paused'

    def _load_speed(self):
        """The speed kept, and the one Resume goes back to: '<speed> [<speed before the pause>]'."""
        self.resume_speed = 'normal'
        try:
            from blink.resources import ApplicationData
            with open(ApplicationData.get(self.speed_file)) as file:
                words = file.read().split()
        except (OSError, ImportError):
            return 'normal'
        speed = words[0] if words and (words[0] in self.speeds or words[0] == 'paused') else 'normal'
        if len(words) > 1 and words[1] in self.speeds:
            self.resume_speed = words[1]
        elif speed in self.speeds:
            self.resume_speed = speed
        return speed

    def resume(self):
        """Resume after Pause, at the speed it had before."""
        self.set_speed(self.resume_speed if self.resume_speed in self.speeds else 'normal')

    def set_speed(self, speed):
        """The user's choice (GUI thread): kept between runs; Paused holds the run until another is chosen."""
        if speed == self.speed or not (speed in self.speeds or speed == 'paused'):
            return
        self.speed = speed
        if speed in self.speeds:
            self.resume_speed = speed
        try:
            from blink.resources import ApplicationData
            with open(ApplicationData.get(self.speed_file), 'w') as file:
                file.write(f'{speed} {self.resume_speed}\n')
        except OSError as e:
            ActivityLog().warning(f'[pgp] Cannot keep the background decryption speed: {e}')
        ActivityLog().info('[pgp] Background decryption paused' if speed == 'paused' else f'[pgp] Background decryption speed: {dict(self.speed_names)[speed]}')
        if self.running:
            self._progress('decrypt', self._total - len(self._queue), self._total)
        if not self.paused and self._held:
            self._held = False
            self._next()

    def handle_notification(self, notification):
        if notification.name == 'SIPApplicationWillEnd':
            self._NH_SIPApplicationWillEnd(notification)    # at once: the GUI may stop handling events while quitting
        else:
            self._handle_in_gui_thread(notification)

    @run_in_gui_thread
    def _handle_in_gui_thread(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkJournalDidApply(self, notification):
        self.run('journal sync')

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, (Account, BonjourAccount)) and 'sms.private_key' in notification.data.modified:
            self._failed.clear()        # a new key may open what the old one could not
            self.run(f'new private key for {notification.sender.id}')

    def _NH_SIPApplicationWillEnd(self, notification):
        left = len(self._queue)
        ActivityLog().info(f'[pgp] Background decryption since start: {self.total_opened} messages decrypted'
                           + (f', {self.total_not_opened} would not decrypt' if self.total_not_opened else '')
                           + (f', {left} left in the run stopped by quitting (reached messages of {self._local_time(self._reached)},'
                              f' {self._rate(self._done, time.time() - self._started_at)} in that run)' if self.running and left else ''))
        self._started = False
        self._queue = []
        self._stop_workers()        # at once: not queued behind a batch in the decrypting thread

    # Running (GUI thread)

    def run(self, reason):
        if not self._started:
            return
        if self.running:
            self._again = True
            return
        accounts = [str(account.id) for account in AccountManager().iter_accounts()
                    if account.enabled and account.sms.enable_pgp and account.sms.private_key is not None
                    and os.path.exists(account.sms.private_key.normalized)]
        if not accounts:
            return
        self.running = True
        self._again = False
        self._done = self._opened = self._not_opened = 0
        self._started_at = time.time()
        self._mark = (self._started_at, 0)     # (time, done) at the last progress line: the recent rate
        self._find(accounts, reason)

    @run_in_thread('db')
    def _find(self, accounts, reason):
        from blink.history import Message
        table = Message.sqlmeta.table
        db = Message._connection
        try:
            rows = db.queryAll(f"select account_id, message_id, timestamp from {table}"
                               f" where account_id in ({', '.join(db.sqlrepr(account) for account in accounts)})"
                               f" and decrypted in ('0', '3') and content like '-----BEGIN PGP MESSAGE-----%'"
                               f" order by timestamp desc")
        except Exception as e:
            ActivityLog().error(f'[pgp] Finding the messages to decrypt failed: {e}')
            call_in_gui_thread(self._finished)
            return
        call_in_gui_thread(self._found, [(str(account_id), str(message_id), timestamp) for account_id, message_id, timestamp in rows], reason)

    def _found(self, queue, reason):
        self._queue = [entry for entry in queue if entry[1] not in self._failed]
        if not self._queue:
            self._finished()
            return
        ActivityLog().info(f'[pgp] Decrypting {len(self._queue)} stored messages in the background ({reason})')
        self._total = len(self._queue)
        self._progress('decrypt', 0, self._total)
        self._next()

    def _progress(self, phase, done=None, total=None):
        """For the main window's progress bar (shared with the journal download, which goes first):
        'decrypt' with done of total messages and the speed, then 'done'."""
        if phase == 'decrypt' and self.running and self._done and not self.paused:
            elapsed = time.time() - self._started_at
            eta = self._eta(len(self._queue), self._done / elapsed if elapsed > 0 else 0)
        else:
            eta = None
        NotificationCenter().post_notification('BlinkBackgroundDecryptionProgress', data=NotificationData(phase=phase, done=done, total=total, speed=self.speed, eta=eta))

    def _next(self):
        if not self._started:
            self.running = False
            self._progress('done')
            return
        if not self._queue:
            self._finished()
            return
        if self.paused:
            self._held = True       # resumed by set_speed
            return
        if time.time() - self.last_view_decrypt < self.busy_pause:
            call_later(self.busy_pause, self._next)     # the pane is opening what is in view: let it have the CPU
            return
        size = self.batch_size * (self._workers_for(self.speed)[0] if self.use_workers else 1)
        batch, self._queue = self._queue[:size], self._queue[size:]
        self._reached = batch[-1][2]
        self._load([(account_id, message_id) for account_id, message_id, _timestamp in batch])

    @staticmethod
    def _eta(left, rate):
        """'ETA 6m 12s (at 11:04)' for left messages at rate per second, or None."""
        if not left or rate <= 0:
            return None
        seconds = int(left / rate)
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        span = f'{hours}h {minutes:02d}m' if hours else (f'{minutes}m {seconds:02d}s' if minutes else f'{seconds}s')
        at = time.strftime('%H:%M', time.localtime(time.time() + left / rate))
        return f'ETA {span} (at {at})'

    @staticmethod
    def _rate(count, seconds):
        """Messages decrypted per second, as 'N.N msg/s'."""
        return f'{count / seconds:.1f} msg/s' if seconds > 0 else '? msg/s'

    @staticmethod
    def _local_time(value):
        """A history timestamp (UTC, naive, as stored) in local time, to the minute."""
        from datetime import datetime, timezone
        if value is None:
            return '?'
        try:
            if not isinstance(value, datetime):
                value = datetime.fromisoformat(str(value))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone().strftime('%Y-%m-%d %H:%M')
        except (TypeError, ValueError):
            return str(value)

    def _finished(self):
        self._progress('done')
        self._release_workers()
        if self._done:
            took = time.time() - self._started_at
            ActivityLog().info(f'[pgp] Background decryption done: {self._opened} messages decrypted'
                               + (f', {self._not_opened} would not decrypt' if self._not_opened else '') + f' in {took:.0f}s ({self._rate(self._done, took)})')
        self.running = False
        if self._again:
            self.run('asked again while running')

    # One batch: contents (db) -> decrypt ('pgp-decrypt') -> store (db) -> next (GUI)

    @run_in_thread('db')
    def _load(self, batch):
        from blink.history import Message
        table = Message.sqlmeta.table
        db = Message._connection
        wanted = {message_id: account_id for account_id, message_id in batch}
        try:
            rows = db.queryAll(f"select account_id, message_id, content from {table}"
                               f" where message_id in ({', '.join(db.sqlrepr(message_id) for message_id in wanted)})"
                               f" and decrypted in ('0', '3')")
        except Exception as e:
            ActivityLog().error(f'[pgp] Reading messages to decrypt failed: {e}')
            rows = []
        work, seen = [], set()
        for account_id, message_id, content in rows:
            message_id, account_id = str(message_id), str(account_id)
            if message_id in seen or wanted.get(message_id) != account_id:
                continue
            if isinstance(content, bytes):
                content = content.decode('utf-8', 'replace')
            seen.add(message_id)
            work.append((account_id, message_id, content))
        self._decrypt(work)

    @run_in_thread('pgp-decrypt')
    def _decrypt(self, work):
        work = [item for item in work if item[2] and '-----BEGIN PGP MESSAGE-----' in item[2]]   # opened meanwhile (in view, or live)
        if self.use_workers and work:
            try:
                results = self._decrypt_in_workers(work)
            except Exception as e:
                self.use_workers = False
                self._stop_workers()
                ActivityLog().warning(f'[pgp] Decrypting in worker processes failed, decrypting in Blink itself: {e!r}')
            else:
                self._store(results)
                return
        self._decrypt_here(work)

    def _pool(self):
        """The workers for the current speed ('pgp-decrypt' thread): started, restarted or stopped to match."""
        count, duty = self._workers_for(self.speed)
        self._workers = [worker for worker in self._workers if worker.alive]
        while len(self._workers) > count:
            self._workers.pop().stop()
        started = 0
        while len(self._workers) < count:
            self._workers.append(DecryptWorker())
            started += 1
        if started:
            ActivityLog().info(f'[pgp] Background decryption: {len(self._workers)} worker processes ({dict(self.speed_names)[self.speed]})')
        for worker in self._workers:
            if worker.duty != duty:
                worker.send({'duty': duty})
                worker.duty = duty
        return self._workers

    def _send_key(self, worker, account_id):
        try:
            account = BonjourAccount() if account_id == BONJOUR_ACCOUNT_ID else AccountManager().get_account(account_id)
        except KeyError:
            return False
        setting = account.sms.private_key
        path = setting.normalized if setting is not None else None
        if not path or not os.path.exists(path):
            return False
        mtime = os.path.getmtime(path)
        if worker.keys.get(account_id) != mtime:
            with open(path, encoding='utf-8', errors='replace') as file:
                worker.send({'account': account_id, 'key': file.read()})
            worker.keys[account_id] = mtime
        return True

    def _decrypt_in_workers(self, work):
        """Spread the messages over the workers, then collect the answers; a message whose worker died
        or hung is left as it is (tried again in a later run)."""
        workers = self._pool()
        share = {worker: [] for worker in workers}
        for index, (account_id, message_id, content) in enumerate(work):
            worker = workers[index % len(workers)]
            try:
                if not self._send_key(worker, account_id):
                    continue        # no key here for it
                worker.send({'id': message_id, 'account': account_id, 'content': content})
            except (BrokenPipeError, OSError):
                continue
            share[worker].append(message_id)
        results = []
        for worker, ids in share.items():
            for _ in ids:
                answer = worker.receive()
                if answer is None:
                    ActivityLog().warning(f'[pgp] A decryption worker stopped answering, restarted for the next batch')
                    worker.stop()
                    break
                if 'plaintext' in answer:
                    results.append((answer['id'], answer['plaintext'], None))
                else:
                    results.append((answer['id'], None, answer.get('error') or 'unknown error'))
        return results

    def _stop_workers(self):
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.stop()

    def _decrypt_here(self, work):
        import pgpy
        results = []
        for account_id, message_id, content in work:
            if not content or '-----BEGIN PGP MESSAGE-----' not in content:
                continue        # opened meanwhile (in view, or live)
            began = time.thread_time()
            key = self.key(account_id)
            if key is None:
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    plaintext = key.decrypt(pgpy.PGPMessage.from_blob(content)).message
                if isinstance(plaintext, (bytes, bytearray)):
                    plaintext = bytes(plaintext).decode('utf-8')
            except Exception as e:
                results.append((message_id, None, str(e) or e.__class__.__name__))
            else:
                results.append((message_id, plaintext, None))
            took = time.thread_time() - began           # CPU this thread spent on the message
            time.sleep(max(self.message_pause, took * (1 / self.duty_cycle - 1)))     # leave the CPU and the interpreter to the rest
        self._store(results)

    @run_in_thread('pgp-decrypt')
    def _release_workers(self):
        self._stop_workers()

    def key(self, account_id):
        """The private key of an account, read again when its file changes ('pgp-decrypt' thread only)."""
        try:
            account = BonjourAccount() if account_id == BONJOUR_ACCOUNT_ID else AccountManager().get_account(account_id)
        except KeyError:
            return None
        setting = account.sms.private_key
        path = setting.normalized if setting is not None else None
        if not path or not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        cached = self._keys.get(account_id)
        if cached is not None and cached[:2] == (path, mtime):
            return cached[2]
        try:
            import pgpy
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                key, _ = pgpy.PGPKey.from_file(path)
        except Exception as e:
            ActivityLog().warning(f'[pgp] Cannot read the private key of {account_id} for background decryption: {e}')
            key = None
        self._keys[account_id] = (path, mtime, key)
        return key

    @run_in_thread('db')
    def _store(self, results):
        from blink.history import MessageHistory
        history = MessageHistory()
        changed, failed = [], []
        for message_id, plaintext, error in results:
            try:
                if plaintext is not None:
                    history.store_decrypted_message(message_id, plaintext)
                else:
                    history.store_undecryptable_message(message_id, error)
                    failed.append((message_id, error))
            except Exception as e:
                ActivityLog().error(f'[pgp] Storing decrypted message {message_id} failed: {e}')
                continue
            changed.append(message_id)
        call_in_gui_thread(self._stored, len(results), changed, failed)

    def _stored(self, count, changed, failed):
        before = self._done
        self._done += count
        self._not_opened += len(failed)
        self._opened += count - len(failed)
        self.total_not_opened += len(failed)
        self.total_opened += count - len(failed)
        for message_id, error in failed:
            self._failed.add(message_id)
            ActivityLog().warning(f'[pgp] Stored message {message_id} would not decrypt: {error}')
        if changed:
            NotificationCenter().post_notification('BlinkMessagesDidDecryptInBackground', sender=self, data=NotificationData(ids=changed))
        self._progress('decrypt', self._total - len(self._queue), self._total)
        if self._done // self.progress_every != before // self.progress_every:
            now = time.time()
            since, before_done = self._mark
            self._mark = (now, self._done)
            recent = (self._done - before_done) / (now - since) if now > since else 0
            overall = self._done / (now - self._started_at) if now > self._started_at else 0
            self.eta = self._eta(len(self._queue), recent or overall)
            ActivityLog().info(f'[pgp] Background messages decryption progress: {self._done} done, {len(self._queue)} left, reached messages of {self._local_time(self._reached)}, '
                               f'{self._rate(self._done - before_done, now - since)} now, {self._rate(self._done, now - self._started_at)} overall'
                               f' ({dict(self.speed_names).get(self.speed, self.speed)}), {self.eta or "ETA unknown"}')
        call_later(self.batch_pause, self._next)
