"""Fetching the files of the messages on screen, as Blink for macOS does.

Only what is in the viewport, 0.3 s after scrolling or a change stops, and only
what auto_fetch_reason allows (pictures to 8 MiB, PDFs to 10 MiB, videos to 20
MiB and audio to 10 MiB when a week old at most; other files never). Skipped too: a file already
here, one that failed before (its folder keeps .failure.json; a click retries
it), one being fetched, and an encrypted one without a key to open it.
Downloads go through SessionManager.get_file_from_url, which also decrypts;
progress is kept here (progress(id) -> 0..1 or None) for the bubble to show,
and the row is refreshed when the file is in place.
"""

import os

from datetime import datetime, timezone

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from blink.logging import ActivityLog, MessagingTrace as log
from blink.message_envelopes import file_transfer_envelope
from blink.messagepane.files import local_file
from blink.messagepane.format import auto_fetch_reason
from blink.util import run_in_gui_thread


__all__ = ['AutoFetcher']


FILE_CATEGORIES = ('image', 'audio', 'video', 'other')


@implementer(IObserver)
class AutoFetcher(QObject):
    delay = 300         # ms

    changed = pyqtSignal(str)       # a message id whose download state changed

    def __init__(self, pane):
        super().__init__(pane)
        self.pane = pane
        self.requested = set()      # message ids asked for in this run
        self._progress = {}         # message id: fraction
        self._files = {}            # File: message id
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.delay)
        self._timer.timeout.connect(self._fetch_visible)
        notification_center = NotificationCenter()
        for name in ('BlinkHTTPTransferProgress', 'BlinkHTTPTransferFailed', 'BlinkHTTPTransferCompleted', 'BlinkFileTransferDidEnd'):
            notification_center.add_observer(self, name=name)

    def schedule(self, *args):
        self._timer.start()

    def progress(self, message_id):
        return self._progress.get(message_id)

    # What is on screen

    def _visible_items(self):
        view = self.pane.transcript
        model = view.model()
        if model is None or not view.isVisible():
            return []
        viewport = view.viewport().rect()
        first = view.indexAt(viewport.topLeft())
        last = view.indexAt(viewport.bottomLeft())
        start = first.row() if first.isValid() else 0
        end = last.row() if last.isValid() else model.rowCount() - 1
        return [model.items[row] for row in range(max(start, 0), min(end, model.rowCount() - 1) + 1)]

    def _fetch_visible(self):
        for item in self._visible_items():
            if item.category in FILE_CATEGORIES and item.id not in self.requested:
                self.fetch(item, automatic=True)

    def fetch(self, item, automatic=False, force=False):
        """Download a message's file. automatic: only within the limits (and quietly)."""
        meta = file_transfer_envelope(item.content) if item.content else None
        if not meta or not meta.get('url') or not meta.get('filename'):
            return
        if local_file(item):
            self.requested.add(item.id)
            return
        size = meta.get('filesize')
        try:
            size = int(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        age_days = (datetime.now(timezone.utc) - item.timestamp).days
        if automatic:
            reason = auto_fetch_reason(item.category, meta['filename'], size, age_days)
            if reason is not None:
                self.requested.add(item.id)
                log.debug(f'Not fetching {meta["filename"]} of message {item.id} on its own: {reason}')
                return
        account = self.pane._account(item.account_id)
        if account is None:
            self.requested.add(item.id)
            return
        if str(meta['filename']).endswith('.asc') and not (account.sms.enable_pgp and account.sms.private_key is not None and os.path.exists(account.sms.private_key.normalized)):
            self.requested.add(item.id)
            log.debug(f'Not fetching encrypted {meta["filename"]}: no key for {account.id}')
            return
        try:
            session = self.pane._message_session()
        except Exception as e:
            log.warning(f'Cannot fetch the file of message {item.id}: {e!r}')
            return
        from blink.configuration.datatypes import File
        from blink.sessions import SessionManager
        until = meta.get('until')
        file = File(meta['filename'], size or 0, session.contact, None, item.id, until, url=meta['url'], type=meta.get('filetype'), account=account, protocol='sylk')
        failure = os.path.join(os.path.dirname(file.name), '.failure.json')
        if automatic and os.path.exists(failure):
            self.requested.add(item.id)
            return          # failed before: only a click tries again
        self.requested.add(item.id)
        self._files[file] = item.id
        self._progress[item.id] = 0.0
        self.changed.emit(item.id)
        ActivityLog().info(f'[transfer] Fetching {os.path.basename(file.name)} ({size or "?"} bytes) of message {item.id}' + (' (in view)' if automatic else ''))
        SessionManager().get_file_from_url(session, file, force=force)

    # Progress

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _message_id(self, file):
        return self._files.get(file) if file is not None else None

    def _NH_BlinkHTTPTransferProgress(self, notification):
        message_id = self._message_id(notification.sender)
        if message_id is None:
            return
        total = notification.data.total_bytes or 0
        fraction = min(1.0, notification.data.bytes / total) if total else None
        if fraction is not None and int(fraction * 20) != int((self._progress.get(message_id) or 0) * 20):
            self._progress[message_id] = fraction
            self.changed.emit(message_id)

    def _NH_BlinkHTTPTransferFailed(self, notification):
        message_id = self._message_id(notification.sender)
        if message_id is None:
            return
        self._files.pop(notification.sender, None)
        self._progress.pop(message_id, None)
        self.changed.emit(message_id)

    def _NH_BlinkHTTPTransferCompleted(self, notification):
        message_id = self._message_id(notification.sender)
        if message_id is not None:
            self._progress[message_id] = 1.0
            self.changed.emit(message_id)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        # after decryption, when there was any: the file is where local_file finds it
        message_id = getattr(notification.data, 'id', None)
        if message_id is None or message_id not in self._progress:
            return
        self._progress.pop(message_id, None)
        for file, known in list(self._files.items()):
            if known == message_id:
                del self._files[file]
        self.changed.emit(message_id)
