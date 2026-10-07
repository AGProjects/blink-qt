"""Files being sent over HTTP, shown in the transcript until their message is in history.

An HTTP transfer (BlinkFileTransfer.route 'http') is a POST to the account's
file transfer service; the server sends the file transfer message on, to the
other party and back to us, and history files it then. Until then the
conversation shows a bubble of its own for the file (MessageItem.upload): the
clock mark while uploading, ✓ once uploaded, ⚠ and the reason when it failed
(a click retries it). The bubble goes when a message of the same transfer
reaches history (settle). The File Transfers window keeps MSRP transfers only.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from PyQt6.QtCore import QObject, pyqtSignal

from blink.logging import ActivityLog
from blink.util import run_in_gui_thread


__all__ = ['Uploads', 'is_http_upload']


def is_http_upload(transfer):
    """An outgoing push over the file transfer service (not MSRP)."""
    return getattr(transfer, 'route', None) == 'http' and getattr(transfer, 'transfer_type', None) == 'push' and getattr(transfer, 'direction', None) == 'outgoing'


@implementer(IObserver)
class Uploads(QObject):
    changed = pyqtSignal(str, str)      # conversation key, transfer id

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.uploads = {}       # transfer id: upload
        notification_center = NotificationCenter()
        for name in ('BlinkFileTransferNewOutgoing', 'BlinkFileTransferWillRetry', 'BlinkFileTransferDidEnd'):
            notification_center.add_observer(self, name=name)

    def for_key(self, key):
        return [upload for upload in self.uploads.values() if upload.key == key]

    def get(self, transfer_id):
        return self.uploads.get(transfer_id)

    def retry(self, transfer_id):
        upload = self.uploads.get(transfer_id)
        if upload is None or upload.state != 'failed':
            return
        ActivityLog().info(f'[Message with {upload.key}] Retrying the upload of {upload.name} ({transfer_id})')
        upload.transfer.connect()

    def discard(self, transfer_id):
        """Cancel an upload in progress, or forget a failed one."""
        upload = self.uploads.pop(transfer_id, None)
        if upload is None:
            return
        if upload.state == 'uploading':
            upload.transfer.end()
        ActivityLog().info(f'[Message with {upload.key}] Upload of {upload.name} ({transfer_id}) ' + ('cancelled' if upload.state == 'uploading' else 'removed'))
        self.changed.emit(upload.key, transfer_id)

    def settle(self, key, transfer_ids):
        """Forget the uploads of key whose message is in history (one of transfer_ids); returns their ids."""
        settled = {upload.id for upload in self.uploads.values() if upload.key == key and upload.id in transfer_ids}
        for transfer_id in settled:
            del self.uploads[transfer_id]
        return settled

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkFileTransferNewOutgoing(self, notification):
        transfer = notification.sender
        if not is_http_upload(transfer):
            return
        import mimetypes
        import os
        from sipsimple.account import AccountManager
        from blink.history import conversation_key
        path = transfer.file_selector.name
        key = conversation_key(str(transfer.contact_uri.uri), AccountManager().default_account)
        self.uploads[transfer.id] = SimpleNamespace(
            id=transfer.id, key=key, transfer=transfer, path=path, name=os.path.basename(path),
            size=transfer.file_selector.size or (os.path.getsize(path) if os.path.exists(path) else None),
            type=transfer.file_selector.type or mimetypes.guess_type(path)[0] or 'application/octet-stream',
            account_id=str(transfer.account.id), timestamp=datetime.now(timezone.utc), state='uploading', reason=None)
        self.changed.emit(key, transfer.id)

    def _NH_BlinkFileTransferWillRetry(self, notification):
        upload = self.uploads.get(getattr(notification.sender, 'id', None))
        if upload is not None and upload.transfer is notification.sender:
            upload.state, upload.reason = 'uploading', None
            self.changed.emit(upload.key, upload.id)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        upload = self.uploads.get(getattr(notification.sender, 'id', None))
        if upload is None or upload.transfer is not notification.sender:
            return
        if notification.data.error:
            upload.state, upload.reason = 'failed', str(notification.data.reason or 'failed')
        else:
            upload.state, upload.reason = 'uploaded', None
        self.changed.emit(upload.key, upload.id)
