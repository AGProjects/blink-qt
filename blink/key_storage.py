# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The PGP key escrow in the SylkServer account storage (keys/pgp-private).

Spec: docs/PRIVATE-KEY-STORAGE.md in SylkServer. The escrow moves from our own
XCAP contact (blink.key_escrow) to the account storage. The object is the same
JSON record the clients write to the `keys` attribute today:

    {"private_key": <encrypted with the account password>, "public_key": <armored>,
     "device": "...", "timestamp": "2026-07-10T12:44:19.049Z"}

Each account keeps its own escrow in its own storage (unlike the avatars of the
shared addressbook, which live in one account's). This module only knows what the
storage holds: it fetches the record, keeps it in memory with its etag, and tells
(BlinkKeyStorageDidChange) when that changes. What to do with it is the escrow's
business (blink.messages.KeyEscrowManager), which writes through store().

A write is conditional on what the storage last said: If-Match its etag, or
If-None-Match: * when it keeps none. A 412 means another device wrote first:
the escrow is fetched again and the escrow pass decides again.

Fetched on the first registration of the account in a run, when its API token
changes, and on application/sylk-storage-changed for keys/pgp-private from
another device. A change notification is not journalled: a device that was
offline learns of it on its next run.

The state of an account:
  pending      asked, no answer yet
  present      the storage keeps an escrow record (record, etag, device, modified)
  absent       it keeps none (404)
  invalid      it keeps something that is not an escrow record
  failed       no answer (network, token, server error): asked again on the next
               registration or token
  unsupported  the account has no SylkServer storage
"""

__all__ = ['KEYS_NAMESPACE', 'KEY_OBJECT', 'KEY_CONTENT_TYPE', 'KeyStorage']

import json
import threading
import urllib.parse

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from sipsimple.account import Account
from sipsimple.threading import run_in_thread
from zope.interface import implementer

from blink.logging import ActivityLog
from blink.message_envelopes import this_device_id
from blink.util import run_in_gui_thread


KEYS_NAMESPACE = 'keys'
KEY_OBJECT = 'pgp-private'
KEY_PATH = f'{KEYS_NAMESPACE}/{KEY_OBJECT}'
KEY_CONTENT_TYPE = 'application/json'

ANSWERED = ('present', 'absent', 'invalid', 'unsupported')


def _writer(response):
    """Who wrote the stored object: its device name (X-Sylk-Device-Name), else its id."""
    name = response.header('X-Sylk-Device-Name')
    return urllib.parse.unquote(name) if name else response.header('X-Sylk-Device-Id')


def _escrow_record(body):
    """The escrow record in a stored object, or None when it is not one."""
    try:
        record = json.loads(body.decode('utf-8') if isinstance(body, (bytes, bytearray)) else body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('private_key'), str) or not isinstance(record.get('public_key'), str):
        return None
    return record


@implementer(IObserver)
class KeyStorage(object, metaclass=Singleton):
    """What the account storage keeps of each account's key escrow."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}           # account id -> dict(state, record, etag, device_id (who stored it: name, else id), modified, reason)
        self.in_flight = set()      # account ids being fetched
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

    # What is known

    def state(self, account):
        with self.lock:
            entry = self.entries.get(account.id)
        return entry['state'] if entry else None

    def answered(self, account):
        """Whether the storage has said what it keeps (or that there is none to ask)."""
        return self.state(account) in ANSWERED

    def entry(self, account):
        """A copy of what is known for the account: dict(state, record, etag, device_id, modified, reason), or None."""
        with self.lock:
            entry = self.entries.get(account.id)
            return dict(entry) if entry else None

    def record(self, account):
        """The escrow record the storage keeps for the account, or None."""
        with self.lock:
            entry = self.entries.get(account.id)
            return dict(entry['record']) if entry and entry['state'] == 'present' else None

    # Asking

    @staticmethod
    def _wanted(account):
        if not isinstance(account, Account) or not account.enabled or not account.sms.enable_pgp:
            return False
        from blink.messages import MessageManager
        return MessageManager._pgp_messaging_possible(account)

    def fetch(self, account, reason):
        """Ask the storage what it keeps (in the background); BlinkKeyStorageDidChange tells the answer."""
        if not self._wanted(account):
            return
        with self.lock:
            if account.id in self.in_flight:
                return
            self.in_flight.add(account.id)
            entry = self.entries.setdefault(account.id, dict(state=None, record=None, etag=None, device_id=None, modified=None, reason=None))
            if entry['state'] is None:
                entry['state'] = 'pending'
            etag = entry['etag'] if entry['state'] == 'present' else None
        ActivityLog().debug(f'[pgp] Key storage of {account.id}: fetching the escrow ({reason})')
        self._fetch(account, etag)

    @run_in_thread('account-storage')
    def _fetch(self, account, etag):
        from blink.account_storage import AccountStorage, storage_url
        try:
            if not storage_url(account):
                self._answer(account, 'unsupported', reason='no SylkServer storage for the account')
                return
            headers = {'If-None-Match': f'"{etag}"'} if etag else {}
            response = AccountStorage()._request(account, 'GET', KEY_PATH, headers=headers)
            if response.status == 200:
                record = _escrow_record(response.body)
                if record is None:
                    self._answer(account, 'invalid', etag=response.etag, device_id=_writer(response),
                                 reason=f'{len(response.body)} bytes of {response.header("Content-Type") or "?"}, not an escrow record')
                else:
                    self._answer(account, 'present', record=record, etag=response.etag, device_id=_writer(response))
            elif response.status == 304:
                self._answer(account, 'present', unchanged=True)
            elif response.status == 404:
                self._answer(account, 'absent')
            else:
                self._answer(account, 'failed', reason=f'HTTP {response.status}' if response.status else 'no answer from the server')
        except Exception as e:
            self._answer(account, 'failed', reason=repr(e))
        finally:
            with self.lock:
                self.in_flight.discard(account.id)

    def _answer(self, account, state, record=None, etag=None, device_id=None, reason=None, unchanged=False):
        note = reason if state == 'failed' else None
        with self.lock:
            entry = self.entries.setdefault(account.id, dict(state=None, record=None, etag=None, device_id=None, modified=None, reason=None))
            previous = (entry['state'], entry['etag'])
            if state == 'failed' and entry['state'] in ANSWERED:
                # what was known stays known: a failed refresh says nothing new about the storage
                entry['reason'] = reason
                state = entry['state']
            elif not unchanged:
                entry.update(state=state, record=record, etag=etag, device_id=device_id, reason=reason)
            changed = previous != (entry['state'], entry['etag'])
            snapshot = dict(entry)
        self._log(account, snapshot, changed, note)
        if changed:
            self._post(account, snapshot)

    @staticmethod
    def _log(account, entry, changed, note):
        activity = ActivityLog()
        state = entry['state']
        if note and not changed:
            if state == 'failed':
                activity.info(f'[pgp] Key storage of {account.id}: could not be read: {note}')
            else:
                activity.info(f'[pgp] Key storage of {account.id}: could not be refreshed, keeping what was known ({note})')
            return
        if not changed:
            return
        if state == 'present':
            from blink.pgp_keyids import key_fingerprint
            record = entry['record'] or {}
            fingerprint = key_fingerprint(record.get('public_key'))
            activity.info(f'[pgp] Key storage of {account.id}: escrow of key {fingerprint[-16:].upper() if fingerprint else "?"}, '
                          f'written {record.get("timestamp") or "?"} by "{record.get("device") or "?"}" '
                          f'(stored by {entry["device_id"] or "?"}, etag {entry["etag"]})')
        elif state == 'absent':
            activity.info(f'[pgp] Key storage of {account.id}: no escrow')
        elif state == 'invalid':
            activity.error(f'[pgp] Key storage of {account.id}: {entry["reason"]}')
        elif state == 'unsupported':
            activity.debug(f'[pgp] Key storage of {account.id}: {entry["reason"]}')
        elif state == 'failed':
            activity.info(f'[pgp] Key storage of {account.id}: could not be read: {entry["reason"]}')

    @run_in_gui_thread
    def _post(self, account, entry):
        NotificationCenter().post_notification('BlinkKeyStorageDidChange', sender=self,
                                               data=NotificationData(account=account, state=entry['state'], entry=entry))

    # Writing

    def writable(self, account):
        """Whether a write can be sent now: the storage has said what it keeps."""
        return self._wanted(account) and self.state(account) in ('present', 'absent')

    def store(self, account, payload, reason):
        """Write the escrow record (its JSON text) in the background. False when it cannot be
        sent now (the storage has not answered)."""
        if not self.writable(account):
            return False
        with self.lock:
            entry = self.entries[account.id]
            condition = {'If-Match': f'"{entry["etag"]}"'} if entry['state'] == 'present' else {'If-None-Match': '*'}
        self._store(account, payload, condition, reason)
        return True

    @run_in_thread('account-storage')
    def _store(self, account, payload, condition, reason):
        from blink.account_storage import AccountStorage
        activity = ActivityLog()
        body = payload.encode('utf-8') if isinstance(payload, str) else payload
        headers = dict(condition, **{'Content-Type': KEY_CONTENT_TYPE, 'X-Sylk-Device-Id': this_device_id()})
        try:
            response = AccountStorage()._request(account, 'PUT', KEY_PATH, body=body, headers=headers)
        except Exception as e:
            activity.error(f'[pgp] Key storage of {account.id}: the escrow could not be written ({reason}): {e!r}')
            return
        if response.status in (200, 201):
            activity.info(f'[pgp] Key storage of {account.id}: escrow {"created" if response.status == 201 else "replaced"} '
                          f'({reason}, {len(body)} bytes, etag {response.etag})')
            self._answer(account, 'present', record=_escrow_record(body), etag=response.etag, device_id=this_device_id())
        elif response.status == 412:
            activity.info(f'[pgp] Key storage of {account.id}: another device wrote the escrow first ({reason}), fetching it again')
            self.fetch(account, 'written by another device meanwhile')
        else:
            activity.error(f'[pgp] Key storage of {account.id}: the escrow could not be written ({reason}): '
                           f'HTTP {response.status or "error"} {response.body[:200]!r}')

    # application/sylk-storage-changed

    def handle_changed(self, account, content):
        """A change of the account storage: only keys/pgp-private concerns us. Never shown, stored or counted."""
        try:
            data = json.loads(content.decode() if isinstance(content, (bytes, bytearray)) else content)
        except Exception:
            return      # the avatars report unreadable ones
        if not isinstance(data, dict) or data.get('namespace') != KEYS_NAMESPACE or data.get('id') != KEY_OBJECT:
            return
        if not isinstance(account, Account):
            return
        device_id = data.get('device_id')
        own = bool(device_id) and device_id == this_device_id()
        ActivityLog().info(f'[pgp] Key storage of {account.id}: the escrow was {"replaced" if data.get("op") == "put" else "deleted"} '
                           f'by {"this device" if own else data.get("device_name") or "device " + str(device_id or "?")}')
        if own:
            return
        self.fetch(account, 'changed on another device')

    # Notifications

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        account = notification.sender
        if self.state(account) in (None, 'failed'):
            self.fetch(account, 'registered')

    def _NH_CFGSettingsObjectDidChange(self, notification):
        account = notification.sender
        modified = getattr(notification.data, 'modified', None) or {}
        if 'sms.history_synchronization_token' in modified and isinstance(account, Account):
            if self.state(account) in (None, 'failed'):
                self.fetch(account, 'new API token')
