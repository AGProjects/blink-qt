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
    speed_names = (('fast', 'Fast'), ('normal', 'Normal'), ('gentle', 'Gentle'), ('paused', 'Paused'))
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
        self.total_opened = self.total_not_opened = 0      # since Blink started, over all runs (logged at quit)
        self.last_view_decrypt = 0.0    # time.time() of the pane's last decrypt_rows
        self.speed = self._load_speed()
        self._held = False          # a batch was due while paused: started on resume

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
        try:
            from blink.resources import ApplicationData
            with open(ApplicationData.get(self.speed_file)) as file:
                speed = file.read().strip()
        except (OSError, ImportError):
            return 'normal'
        return speed if speed in self.speeds or speed == 'paused' else 'normal'

    def set_speed(self, speed):
        """The user's choice (GUI thread): kept between runs; Paused holds the run until another is chosen."""
        if speed == self.speed or not (speed in self.speeds or speed == 'paused'):
            return
        self.speed = speed
        try:
            from blink.resources import ApplicationData
            with open(ApplicationData.get(self.speed_file), 'w') as file:
                file.write(speed + '\n')
        except OSError as e:
            ActivityLog().warning(f'[pgp] Cannot keep the background decryption speed: {e}')
        ActivityLog().info(f'[pgp] Background decryption speed: {dict(self.speed_names)[speed]}')
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
                           + (f', {left} left in the run stopped by quitting (reached messages of {self._local_time(self._reached)})' if self.running and left else ''))
        self._started = False
        self._queue = []

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
        NotificationCenter().post_notification('BlinkBackgroundDecryptionProgress', data=NotificationData(phase=phase, done=done, total=total, speed=self.speed))

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
        batch, self._queue = self._queue[:self.batch_size], self._queue[self.batch_size:]
        self._reached = batch[-1][2]
        self._load([(account_id, message_id) for account_id, message_id, _timestamp in batch])

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
        if self._done:
            took = time.time() - self._started_at
            ActivityLog().info(f'[pgp] Background decryption done: {self._opened} messages decrypted'
                               + (f', {self._not_opened} would not decrypt' if self._not_opened else '') + f' in {took:.0f}s')
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
            ActivityLog().info(f'[pgp] Background messages decryption progress: {self._done} done, {len(self._queue)} left, reached messages of {self._local_time(self._reached)}')
        call_later(self.batch_pause, self._next)
