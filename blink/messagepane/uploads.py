"""Files being sent over HTTP, shown in the transcript until their message is in history.

An HTTP transfer (BlinkFileTransfer.route 'http') is a POST to the account's
file transfer service; the server sends the file transfer message on, to the
other party and back to us, and history files it then. Until then the
conversation shows a bubble of its own for the file (MessageItem.upload): the
clock mark while uploading, ✓ once uploaded, ⚠ and the reason when it failed
(a click retries it); while encrypting and uploading it says so, with the percentage
uploaded, and its Cancel button (or Cancel Sending) cancels it, also on the server,
removing the copy it was sent from. The bubble goes, once the file is uploaded, when a message
of the same transfer is in history (settle). The File Transfers window keeps MSRP
transfers only.

The message is filed in history as soon as the upload starts (a local copy,
MessageHistory.add_own_upload), as a text is when it is sent: the conversation
gets its time and moves up in the contact list. Its state follows the upload
(pending, accepted, failed-local; deleted when the upload is discarded), and the
server's message, when it comes back, completes it with the URL instead of being
stored as a new one (MessageManager.note_own_upload).
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
        for name in ('BlinkFileTransferNewOutgoing', 'BlinkFileTransferWillRetry', 'BlinkFileTransferDidEnd', 'BlinkFileTransferProgress'):
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
        self._remove_copy(upload)
        ActivityLog().info(f'[Message with {upload.key}] Upload of {upload.name} ({transfer_id}) ' + ('cancelled' if upload.state == 'uploading' else 'removed'))
        # the local copy goes with it, as a deleted message (not only marked so): the conversation's
        # time is that of its newest live message, so it falls back to what it was before the upload
        # and the contact moves back to its place in the list
        from blink.history import ConversationPreviews, MessageHistory
        MessageHistory().tombstone_message(transfer_id, account_id=upload.account_id, remote_uri=upload.key, source='upload cancelled')
        ConversationPreviews().invalidate([upload.key])     # read after the removal (the same db thread)
        self.changed.emit(upload.key, transfer_id)

    @staticmethod
    def _remove_copy(upload):
        """The copy the file was sent from (BlinkFileTransfer._own_copy) goes with the message: only
        the folder of this transfer in Blink's storage, never a file elsewhere."""
        import os
        import shutil
        from blink.resources import ApplicationData
        folder = os.path.realpath(os.path.dirname(upload.path or ''))
        root = os.path.realpath(ApplicationData.get('file_transfers'))
        if os.path.basename(folder) == upload.id and folder.startswith(root + os.sep) and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)

    def settle(self, key, transfer_ids):
        """Forget the uploads of key that are uploaded and whose message is in history (one of
        transfer_ids); returns their ids. One still uploading (or failed) keeps its bubble, though
        its local copy is in history from the start."""
        settled = {upload.id for upload in self.uploads.values() if upload.key == key and upload.id in transfer_ids and upload.state == 'uploaded'}
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
            account_id=str(transfer.account.id), timestamp=datetime.now(timezone.utc), state='uploading', reason=None,
            stage=None, progress=None)     # stage: encrypting or uploading; progress: the fraction uploaded
        upload = self.uploads[transfer.id]
        # the local copy, as for any message sent (confirmed by the server's message later)
        from blink.history import MessageHistory
        from blink.messages import MessageManager
        MessageManager().note_own_upload(transfer.id)
        contact = getattr(transfer, 'contact', None)
        MessageHistory().add_own_upload(transfer.id, conversation_key(str(transfer.contact_uri.uri), transfer.account), transfer.account,
                                        str(transfer.contact_uri.uri), getattr(contact, 'name', '') or '', upload.name, upload.size, upload.type,
                                        upload.timestamp.replace(tzinfo=None))
        self.changed.emit(key, transfer.id)

    def _NH_BlinkFileTransferProgress(self, notification):
        """Encrypting, then the upload's progress, as mobile shows them."""
        upload = self.uploads.get(getattr(notification.sender, 'id', None))
        if upload is None or upload.transfer is not notification.sender or upload.state != 'uploading':
            return
        data = notification.data
        stage = getattr(data, 'stage', None) or 'uploading'
        total = getattr(data, 'total_bytes', None)
        progress = data.bytes / total if total and getattr(data, 'bytes', None) is not None else None
        if (stage, None if progress is None else int(progress * 100)) == (upload.stage, None if upload.progress is None else int(upload.progress * 100)):
            return
        upload.stage, upload.progress = stage, progress
        self.changed.emit(upload.key, upload.id)

    def _NH_BlinkFileTransferWillRetry(self, notification):
        upload = self.uploads.get(getattr(notification.sender, 'id', None))
        if upload is not None and upload.transfer is notification.sender:
            upload.state, upload.reason = 'uploading', None
            from blink.history import MessageHistory
            MessageHistory().update(upload.id, 'pending')
            self.changed.emit(upload.key, upload.id)

    def _NH_BlinkFileTransferDidEnd(self, notification):
        upload = self.uploads.get(getattr(notification.sender, 'id', None))
        if upload is None or upload.transfer is not notification.sender:
            return
        if notification.data.error:
            upload.state, upload.reason = 'failed', str(notification.data.reason or 'failed')
        else:
            upload.state, upload.reason = 'uploaded', None
        from blink.history import MessageHistory
        MessageHistory().update(upload.id, 'accepted' if upload.state == 'uploaded' else 'failed-local')
        self.changed.emit(upload.key, upload.id)
