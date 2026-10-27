"""SylkServer account storage, and the contact avatars kept in it.

Spec: docs/CONTACT-AVATARS.md. Account storage is a private per-account object
store on SylkServer, reached over HTTPS with the account's API token (the one
application/sylk-api-token hands out, the journal's). Changes are announced by
an application/sylk-storage-changed MESSAGE from the account to itself.

Avatars live in the `avatars` namespace under the contact's own id: one object
per contact, replaced in place when the picture changes. Nothing is written to
XCAP. Which contacts have a picture, and which version, is what the storage
says: the namespace listing when the addressbook settles and on
re-registration, and the change notifications in between. The local cache
mirrors that, so a contact has a stored picture exactly when the cache holds
one under its id.

Pictures per contact on this device (blink.resources.IconManager):
  <id>_alt   set by the user in the contact editor (contact.alternate_icon)
  <id>       the icon the contact publishes with its presence

The user is asked to choose between a server picture and one set on this device
only at the first sync of each account (first_sync_done). After it, a picture set
here that has not been uploaded is newer than the server's, and is uploaded.

Precedence when a contact is drawn (contact_picture):
  1. a picture the user just set, still waiting to be uploaded (avatar_pending)
  2. the stored avatar, from the cache below
  3. the user-set picture (an account without storage), else the presence icon
  4. the initials
"""

__all__ = ['STORAGE_CHANGED_CONTENT_TYPE', 'AVATARS_NAMESPACE', 'AccountStorage', 'contact_picture',
           'storage_url', 'storage_account', 'encode_avatar', 'valid_name']

import json
import os
import re
import threading
import time
import urllib.parse

from PyQt6.QtCore import QBuffer, QIODevice, QRect, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from application.system import makedirs, unlink
from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.addressbook import AddressbookManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from zope.interface import implementer

from blink.file_transfer import FILE_TRANSFER_PATH
from blink.logging import ActivityLog
from blink.message_envelopes import STORAGE_CHANGED_CONTENT_TYPE, this_device_id
from blink.resources import ApplicationData, IconManager
from blink.util import call_later, run_in_gui_thread, translate


AVATARS_NAMESPACE = 'avatars'

AVATAR_PIXELS = 96
AVATAR_JPEG_QUALITY = 70
AVATAR_CONTENT_TYPE = 'image/jpeg'

# An object no contact owns is deleted only once it is this old: the contact of a
# fresh upload may not have reached this device's addressbook yet.
GC_MIN_AGE_MS = 7 * 24 * 3600 * 1000

# A re-list on re-registration at most this often: registration refreshes are
# frequent, and a notification missed while disconnected is all a re-list catches.
SYNC_MIN_INTERVAL = 600.0

# How long a request refused with 401 waits for the new token before its one retry.
TOKEN_WAIT = 20.0

PENDING_RETRY_INTERVAL = 300.0

_name_re = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$')


def valid_name(name):
    return bool(name) and bool(_name_re.match(str(name)))


def _api_root(account):
    """<base>/webrtcgateway: the root the file transfers hang off."""
    from blink.messages import MessageManager
    try:
        base = MessageManager().file_transfer_base_url(account)
    except Exception:
        base = None
    base = str(base or '').rstrip('/')
    if base.endswith(FILE_TRANSFER_PATH):
        return base[:-len(FILE_TRANSFER_PATH)]
    return None


def storage_url(account):
    """{storage_url} of an account, or None when it has no SylkServer."""
    if account is None or account is BonjourAccount() or not isinstance(account, Account):
        return None
    root = _api_root(account)
    if not root:
        return None
    return '%s/storage/%s' % (root, urllib.parse.quote(str(account.id).lower(), safe='@'))


def _token(account):
    token = getattr(account.sms, 'history_synchronization_token', None)
    return str(token) if token else None


def storage_account():
    """The account whose storage holds the avatars of the shared addressbook.

    The addressbook is replicated to every XCAP account, an avatar object lives in
    one storage: the default account when it qualifies (enabled, XCAP, a SylkServer
    behind it), otherwise the first that does. None: avatars stay on this device.
    """
    try:
        manager = AccountManager()
        accounts = [account for account in manager.get_accounts()
                    if isinstance(account, Account) and account.enabled and account.xcap.enabled and storage_url(account)]
    except Exception:
        return None
    if not accounts:
        return None
    default = manager.default_account
    return default if default in accounts else accounts[0]


def user_picture_path(contact_id):
    """The picture the user set for a contact in the contact editor (IconManager's <id>_alt)."""
    return ApplicationData.get(os.path.join('images', str(contact_id).replace('/', '_') + '_alt.png'))


def has_user_picture(contact):
    return getattr(contact, 'alternate_icon', None) is not None and os.path.isfile(user_picture_path(contact.id))


def encode_avatar(source):
    """A picture as the avatar bytes: 96x96 square centre crop, flattened onto white, JPEG 70.

    source is a file name, bytes or a QImage. Drawn into a fresh image, so nothing of
    the source's metadata survives. QImage only, so it may run off the GUI thread.
    Returns bytes, or None.
    """
    if isinstance(source, QImage):
        image = source
    elif isinstance(source, (bytes, bytearray)):
        image = QImage()
        image.loadFromData(bytes(source))
    else:
        image = QImage(str(source)) if source else QImage()
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return None
    side = min(image.width(), image.height())
    square = image.copy(QRect((image.width() - side) // 2, (image.height() - side) // 2, side, side))
    scaled = square.scaled(AVATAR_PIXELS, AVATAR_PIXELS, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    flat = QImage(AVATAR_PIXELS, AVATAR_PIXELS, QImage.Format.Format_RGB32)
    flat.fill(QColor(255, 255, 255))
    painter = QPainter(flat)
    painter.drawImage(0, 0, scaled)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not flat.save(buffer, 'JPEG', AVATAR_JPEG_QUALITY):
        return None
    return bytes(buffer.data())


def contact_picture(contact):
    """The picture a contact is drawn with (a QIcon), or None for the initials. GUI thread."""
    contact_id = getattr(contact, 'id', None)
    if not contact_id:
        return None
    icon_manager = IconManager()
    user = icon_manager.get(contact_id + '_alt') if getattr(contact, 'alternate_icon', None) is not None else None
    if user is not None and getattr(contact, 'avatar_pending', False):
        return user
    stored = AccountStorage().avatar_icon(contact)
    if stored is not None:
        return stored
    return user or icon_manager.get(contact_id)


class _Response(object):
    def __init__(self, status, headers=None, body=b''):
        self.status = status
        self.headers = dict(headers or {})
        self.body = body or b''

    def json(self):
        try:
            return json.loads(self.body.decode())
        except Exception:
            return None

    def header(self, name):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    @property
    def etag(self):
        value = self.header('ETag') or ''
        if value.startswith('W/'):
            value = value[2:]
        return value.strip('"')


class AvatarCache(object):
    """{etag, content_type, bytes} per avatar id, per account, on disk.

    account_storage/<account>/avatars/<id> holds the bytes, index.json beside them
    the etag, content type, size, uploader (device_id) and upload time (modified).
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.indexes = {}

    def _dir(self, account_id):
        return ApplicationData.get(os.path.join('account_storage', str(account_id).lower(), AVATARS_NAMESPACE))

    def _index(self, account_id):
        key = str(account_id).lower()
        with self.lock:
            index = self.indexes.get(key)
            if index is None:
                try:
                    with open(os.path.join(self._dir(key), 'index.json')) as f:
                        index = json.load(f)
                    if not isinstance(index, dict):
                        index = {}
                except (OSError, ValueError):
                    index = {}
                self.indexes[key] = index
            return index

    def _save_index(self, account_id):
        key = str(account_id).lower()
        directory = self._dir(key)
        makedirs(directory)
        path = os.path.join(directory, 'index.json')
        with open(path + '.part', 'w') as f:
            json.dump(self.indexes.get(key, {}), f)
        os.replace(path + '.part', path)

    def get(self, account_id, object_id):
        """(etag, path) of a cached avatar, or None."""
        with self.lock:
            entry = self._index(account_id).get(object_id)
            if not entry:
                return None
            path = os.path.join(self._dir(account_id), object_id)
            if not os.path.isfile(path):
                return None
            return entry.get('etag'), path

    def put(self, account_id, object_id, etag, content_type, data, meta=None):
        """meta: device_id (the uploader) and modified (ms), when known."""
        if not valid_name(object_id):
            return None
        with self.lock:
            directory = self._dir(account_id)
            makedirs(directory)
            path = os.path.join(directory, object_id)
            with open(path + '.part', 'wb') as f:
                f.write(data)
            os.replace(path + '.part', path)
            entry = {'etag': etag, 'content_type': content_type, 'size': len(data)}
            for key in ('device_id', 'modified'):
                if meta and meta.get(key):
                    entry[key] = meta[key]
            self._index(account_id)[object_id] = entry
            self._save_index(account_id)
            return path

    def meta(self, account_id, object_id):
        with self.lock:
            return dict(self._index(account_id).get(object_id) or {})

    def drop(self, account_id, object_id):
        with self.lock:
            known = self._index(account_id).pop(object_id, None) is not None
            if valid_name(object_id):
                unlink(os.path.join(self._dir(account_id), object_id))
            if known:
                self._save_index(account_id)
            return known

    def ids(self, account_id):
        with self.lock:
            return set(self._index(account_id))

    def accounts(self):
        try:
            return os.listdir(ApplicationData.get('account_storage'))
        except OSError:
            return []

    def _state_path(self, account_id):
        return os.path.join(os.path.dirname(self._dir(account_id)), 'state.json')

    def state(self, account_id):
        try:
            with open(self._state_path(account_id)) as f:
                value = json.load(f)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_state(self, account_id, **values):
        with self.lock:
            state = self.state(account_id)
            state.update(values)
            path = self._state_path(account_id)
            makedirs(os.path.dirname(path))
            with open(path + '.part', 'w') as f:
                json.dump(state, f)
            os.replace(path + '.part', path)


@implementer(IObserver)
class AccountStorage(object, metaclass=Singleton):
    """The client side of /webrtcgateway/storage, and the avatar sync on it."""

    def __init__(self):
        self.cache = AvatarCache()
        self.lock = threading.RLock()
        self.missing = set()        # (account, id) the server answered 404 for
        self.in_flight = set()      # (account, id) being fetched
        self.uploading = set()      # contact ids being uploaded
        self.last_sync = {}         # account id: time of the last listing
        self.settled = set()        # accounts whose avatars have been synced since their addressbook settled
        self.addressbooks = set()   # accounts whose addressbook has settled (BlinkAddressbookDidSettle)
        self.logged_urls = set()
        self.icons = {}             # contact id: (path, etag, QIcon), GUI thread
        self.started = False
        self.asking = False         # the choice window is on screen

    def start(self):
        if self.started:
            return
        self.started = True
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkAddressbookDidSettle')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        self._schedule_pending_retry()

    # notifications

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkAddressbookDidSettle(self, notification):
        self.addressbooks.update(getattr(notification.data, 'accounts', None) or ())
        self._addressbook_settled()

    def _addressbook_settled(self):
        # also when the storage account becomes usable after its addressbook settled (its first token)
        account = storage_account()
        if account is None or account.id not in self.addressbooks or not _token(account):
            return
        first = account.id not in self.settled
        self.settled.add(account.id)
        if first or time.time() - self.last_sync.get(account.id, 0) >= SYNC_MIN_INTERVAL:
            self.sync(account)
        self.retry_pending()

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        # a reconnect: the change notifications sent meanwhile are not journalled, so re-list;
        # never before the addressbook has settled (the garbage collection decides on which contacts exist)
        account = notification.sender
        if account.id not in self.settled or account is not storage_account():
            return
        if time.time() - self.last_sync.get(account.id, 0) >= SYNC_MIN_INTERVAL:
            self.sync(account)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        modified = getattr(notification.data, 'modified', None) or {}
        if 'sms.history_synchronization_token' in modified or 'sms.history_synchronization_url' in modified:
            account = notification.sender
            if account is storage_account() and _token(account):
                if account.id not in self.settled:
                    self._addressbook_settled()
                else:
                    self.retry_pending()

    def _schedule_pending_retry(self):
        def tick():
            self.retry_pending()
            self._schedule_pending_retry()
        call_later(PENDING_RETRY_INTERVAL, tick)

    @run_in_gui_thread
    def _post_changed(self, ids):
        """Redraw the contacts whose stored picture changed: the views follow their contact."""
        ids = set(ids)
        for contact_id in ids:
            self.icons.pop(contact_id, None)
        notification_center = NotificationCenter()
        for contact in self._contacts():
            if contact.id in ids:
                notification_center.post_notification('BlinkContactAvatarDidChange', sender=contact)
        notification_center.post_notification('BlinkContactAvatarsDidChange', sender=self, data=NotificationData(ids=ids))

    # HTTP

    def _request(self, account, method, path='', body=None, headers=None, retry=True):
        """One request to {storage_url}/path, synchronously, off the GUI thread.

        A 401 asks the server for a new token, the common way, waits for it and retries once.
        """
        import requests
        base = storage_url(account)
        if not base:
            return _Response(0)
        token = _token(account)
        if not token:
            self._ask_token(account, 'account storage needs one')
            return _Response(401)
        url = base + ('/' + path if path else '')
        if base not in self.logged_urls:
            self.logged_urls.add(base)
            ActivityLog().info(f'[avatar] Account storage of {account.id} at {base}')
        request_headers = {'Authorization': f'Apikey {token}'}
        request_headers.update((name, value) for name, value in (headers or {}).items() if value is not None)
        try:
            response = requests.request(method, url, data=body, headers=request_headers, timeout=(15, 30),
                                        verify=SIPSimpleSettings().tls.verify_server)
            result = _Response(response.status_code, response.headers, response.content)
        except requests.RequestException as e:
            ActivityLog().info(f'[avatar] Account storage {method} {url} failed: {e}')
            return _Response(0)
        if result.status == 401 and retry:
            ActivityLog().info(f'[avatar] Account storage token of {account.id} refused, requesting a new one')
            self._ask_token(account, 'account storage refused the token (401)')
            deadline = time.time() + TOKEN_WAIT
            while time.time() < deadline:
                time.sleep(0.5)
                if _token(account) and _token(account) != token:
                    break
            return self._request(account, method, path, body, headers, retry=False)
        if result.status >= 500:
            ActivityLog().error(f'[avatar] Account storage {method} {url}: HTTP {result.status} {result.body[:200]!r}')
        return result

    @run_in_gui_thread
    def _ask_token(self, account, reason):
        from blink.messages import MessageManager
        MessageManager()._request_history_synchronization_token(account, reason)

    @staticmethod
    def device_id():
        return this_device_id()

    # avatars: reading

    def avatar_path(self, contact):
        """The cached stored picture of a contact, or None. Never touches the network:
        the cache is kept equal to the server by sync() and handle_changed()."""
        object_id = str(getattr(contact, 'id', '') or '')
        if not valid_name(object_id):
            return None
        account = storage_account()
        if account is not None:
            found = self.cache.get(account.id, object_id)
            return found[1] if found else None
        # no storage account yet, early in a start: the ids are contact ids, any copy is this contact's
        for directory in self.cache.accounts():
            found = self.cache.get(directory, object_id)
            if found:
                return found[1]
        return None

    def avatar_icon(self, contact):
        """The stored picture of a contact as a QIcon, or None. GUI thread."""
        path = self.avatar_path(contact)
        if path is None:
            self.icons.pop(contact.id, None)
            return None
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return None
        cached = self.icons.get(contact.id)
        if cached is not None and cached[0] == path and cached[1] == stamp:
            return cached[2]
        pixmap = QPixmap(path)
        icon = QIcon(pixmap) if not pixmap.isNull() else None
        if icon is not None:
            icon.filename = path
        self.icons[contact.id] = (path, stamp, icon)
        return icon

    def avatar_info(self, contact):
        """(uploader, modified ms) of a contact's stored picture, or None."""
        account = storage_account()
        if account is None or self.cache.get(account.id, contact.id) is None:
            return None
        meta = self.cache.meta(account.id, contact.id)
        return self.device_label(meta.get('device_id')), meta.get('modified')

    def device_label(self, device_id):
        """Who uploaded a picture, for a human: this computer, or another device by its id."""
        if not device_id:
            return translate('account_storage', 'unknown device')
        if device_id == self.device_id():
            return translate('account_storage', 'This computer')
        return translate('account_storage', 'Device %s') % str(device_id)[:8]

    def fetch(self, account, object_id, cached_etag=None, force=False, meta=None):
        key = (account.id, object_id)
        with self.lock:
            if key in self.in_flight or (key in self.missing and not force):
                return
            self.in_flight.add(key)
        self._fetch(account, object_id, cached_etag, meta)

    @run_in_thread('account-storage')
    def _fetch(self, account, object_id, cached_etag, meta=None):
        self._get_avatar(account, object_id, cached_etag, meta)

    def _get_avatar(self, account, object_id, cached_etag=None, meta=None):
        """GET one avatar into the cache, synchronously. True when it is cached."""
        key = (account.id, object_id)
        try:
            headers = {'If-None-Match': f'"{cached_etag}"'} if cached_etag else {}
            response = self._request(account, 'GET', f'{AVATARS_NAMESPACE}/{object_id}', headers=headers)
            if response.status == 200:
                content_type = (response.header('Content-Type') or '').split(';')[0].strip()
                info = dict(meta or {})
                info['device_id'] = response.header('X-Sylk-Device-Id') or info.get('device_id')
                self.cache.put(account.id, object_id, response.etag, content_type, response.body, info)
                with self.lock:
                    self.missing.discard(key)
                ActivityLog().debug(f'[avatar] Avatar {object_id} fetched ({len(response.body)} bytes)')
                self._post_changed([object_id])
                return True
            elif response.status == 304:
                return True
            elif response.status == 404:
                with self.lock:
                    self.missing.add(key)
                if self.cache.drop(account.id, object_id):
                    self._post_changed([object_id])
            else:
                ActivityLog().info(f'[avatar] Cannot fetch avatar {object_id}: HTTP {response.status or "error"}')
            return False
        finally:
            with self.lock:
                self.in_flight.discard(key)

    # avatars: sync

    @staticmethod
    def _contacts():
        try:
            return AddressbookManager().get_contacts()
        except Exception:
            return []

    def first_sync_done(self, account):
        """Whether this device has been through its first avatar sync with the account:
        the listing read and the choice, if any, confirmed. Kept on disk, per account."""
        return bool(self.cache.state(account.id).get('first_sync_done'))

    def _first_sync_finished(self, account):
        if not self.first_sync_done(account):
            self.cache.set_state(account.id, first_sync_done=int(time.time()))
            ActivityLog().info(f'[avatar] First avatar sync of {account.id} done: from now on a picture set here replaces the server one, nobody is asked')

    @run_in_gui_thread
    def sync(self, account):
        self.last_sync[account.id] = time.time()
        contacts = self._contacts()
        contact_ids = {contact.id for contact in contacts}
        # pictures the user set on this device that were never uploaded; presence icons never count
        unsent = [contact for contact in contacts if valid_name(contact.id) and has_user_picture(contact)
                  and not getattr(contact, 'avatar_pending', False) and not getattr(contact, 'avatar_delete_pending', False)]
        if unsent and self.first_sync_done(account):
            # after the first sync there is nothing to choose: a picture set here is newer than
            # the server's (set while the storage was not reachable), so it is uploaded
            for contact in unsent:
                contact.avatar_pending = True
                contact.save()
                self.upload_avatar(contact, reason='set on this computer, not uploaded yet')
            unsent = []
        # ours to settle first: a picture not uploaded yet must not be overwritten by the
        # old one, a removed one must not come back
        pending = {contact.id for contact in contacts if getattr(contact, 'avatar_pending', False) or getattr(contact, 'avatar_delete_pending', False)}
        # first sync only: where the server has a picture too the user chooses, where it has
        # none the local one is pushed; either way the local file goes, so once per contact
        local = {contact.id: user_picture_path(contact.id) for contact in unsent}
        self._sync(account, contact_ids, pending, local)

    @run_in_thread('account-storage')
    def _sync(self, account, contact_ids, pending, local):
        """Make the cache equal to the server, and collect orphans.

        1. GET {storage_url}/avatars
        2. a contact's object missing from the cache or with another etag: GET it
        3. cached ids not on the server, or whose contact is gone: drop
        4. objects no contact owns, older than GC_MIN_AGE_MS: DELETE
        """
        response = self._request(account, 'GET', AVATARS_NAMESPACE)
        if response.status != 200:
            ActivityLog().info(f'[avatar] Avatar sync of {account.id} skipped: the listing answered HTTP {response.status or "error"}')
            return
        listing = response.json() or {}
        objects = {item.get('id'): item for item in listing.get('objects') or [] if isinstance(item, dict) and item.get('id')}
        with self.lock:
            self.missing = {key for key in self.missing if key[0] != account.id}

        conflicts = []
        if not self.asking:
            # fetched now, before the question: the user has to see the server picture to choose it
            for object_id in sorted(set(objects) & set(local)):
                cached = self.cache.get(account.id, object_id)
                if cached is None or cached[0] != objects[object_id].get('etag'):
                    if not self._get_avatar(account, object_id, cached[0] if cached else None, self._listing_meta(objects[object_id])):
                        continue
                conflicts.append(object_id)

        to_fetch = []
        for object_id, meta in objects.items():
            if object_id not in contact_ids or object_id in pending or object_id in conflicts:
                continue
            cached = self.cache.get(account.id, object_id)
            if cached is None or cached[0] != meta.get('etag'):
                to_fetch.append((object_id, cached[0] if cached else None, self._listing_meta(meta)))

        dropped = []
        for object_id in self.cache.ids(account.id):
            if object_id not in objects or object_id not in contact_ids:
                self.cache.drop(account.id, object_id)
                dropped.append(object_id)

        now_ms = int(time.time() * 1000)
        collected = 0
        for object_id, meta in objects.items():
            if object_id in contact_ids or now_ms - int(meta.get('modified') or 0) < GC_MIN_AGE_MS:
                continue
            gone = self._request(account, 'DELETE', f'{AVATARS_NAMESPACE}/{object_id}',
                                 headers={'If-Match': f'"{meta.get("etag")}"', 'X-Sylk-Device-Id': self.device_id()})
            if gone.status in (204, 404):
                collected += 1
        if to_fetch or dropped or collected:
            ActivityLog().info(f'[avatar] Avatar sync of {account.id}: {len(objects)} on the server, {len(to_fetch)} to fetch, '
                               f'{len(dropped)} dropped from the cache, {collected} orphans deleted')
        if dropped:
            self._post_changed(dropped)
        for object_id, cached_etag, meta in to_fetch:
            self.fetch(account, object_id, cached_etag=cached_etag, force=True, meta=meta)
        push = sorted(object_id for object_id in local if object_id not in objects and object_id in contact_ids)
        if push:
            self._push(account, push)
        if conflicts:
            ActivityLog().info(f'[avatar] {len(conflicts)} contacts of {account.id} have a picture both on the server and on this computer, asking which to keep')
            self._ask_conflicts(account, [(object_id, local[object_id]) for object_id in conflicts])
        elif not self.asking:
            self._first_sync_finished(account)

    @staticmethod
    def _listing_meta(meta):
        return {'device_id': meta.get('device_id'), 'modified': meta.get('modified')}

    @run_in_gui_thread
    def _push(self, account, contact_ids):
        """Nothing on the server: the pictures set on this device go up."""
        contacts = {contact.id: contact for contact in self._contacts()}
        started = 0
        for contact_id in contact_ids:
            contact = contacts.get(contact_id)
            if contact is None or getattr(contact, 'avatar_pending', False):
                continue
            contact.avatar_pending = True       # first, so a failed upload is retried like any other
            contact.save()
            if self.upload_avatar(contact, reason='set on this computer, none on the server'):
                started += 1
        ActivityLog().info(f'[avatar] Uploading {started} pictures set on this computer for contacts of {account.id} that have none on the server')

    # avatars: the choice between the server and this device

    @run_in_gui_thread
    def _ask_conflicts(self, account, conflicts):
        """Show both pictures per contact and apply what the user keeps.

        Every choice removes the local picture (kept: uploaded, then removed; not kept:
        removed), so a contact is asked about once. Quitting before Confirm asks again
        at the next sync.
        """
        if self.asking:
            return
        if QApplication.activeModalWidget() is not None:
            call_later(5, self._ask_conflicts, account, conflicts)     # another question is up: after it
            return
        contacts = {contact.id: contact for contact in self._contacts()}
        items = []
        for object_id, local_path in conflicts:
            contact = contacts.get(object_id)
            cached = self.cache.get(account.id, object_id)
            if contact is None or cached is None or not os.path.isfile(local_path):
                continue
            meta = self.cache.meta(account.id, object_id)
            try:
                local_time = os.path.getmtime(local_path)
            except OSError:
                local_time = None
            server_time = (meta.get('modified') or 0) / 1000.0
            detail = self._default_uri(contact)
            items.append({'contact_id': object_id,
                          'name': contact.name or detail or object_id,
                          'detail': detail,
                          'server_path': cached[1],
                          'server_device': self.device_label(meta.get('device_id')),
                          'server_time': meta.get('modified'),
                          'local_path': local_path,
                          'local_time': local_time,
                          'default': 'local' if local_time and local_time > server_time else 'server'})
        if not items:
            self._first_sync_finished(account)
            return
        items.sort(key=lambda item: item['name'].lower())
        self.asking = True
        try:
            from blink.avatar_choice import AvatarChoiceDialog
            choice = AvatarChoiceDialog(items, str(account.id)).choose()
        finally:
            self.asking = False
        kept_server = sum(1 for side in choice.values() if side == 'server')
        ActivityLog().info(f'[avatar] Pictures chosen for {len(choice)} contacts: {kept_server} kept from the server, {len(choice) - kept_server} from this computer')
        for contact_id, side in choice.items():
            contact = contacts.get(contact_id)
            if contact is None:
                continue
            if side == 'server':
                IconManager().remove(contact.id + '_alt')
                contact.alternate_icon = None
                contact.save()
            else:
                # replaces the server copy for every device; shown from the local file until uploaded
                contact.avatar_pending = True
                contact.save()
                self.upload_avatar(contact, reason='kept over the server picture')
        self._first_sync_finished(account)
        self._post_changed(list(choice))

    # avatars: live sync

    def handle_changed(self, account, content):
        """application/sylk-storage-changed: never shown, stored or counted."""
        try:
            data = json.loads(content.decode() if isinstance(content, bytes) else content)
        except Exception as e:
            ActivityLog().info(f'[avatar] Cannot read a storage change: {e}')
            return
        if not isinstance(data, dict) or data.get('namespace') != AVATARS_NAMESPACE:
            return
        if data.get('device_id') and data.get('device_id') == self.device_id():
            return
        if account is None or account is not storage_account():
            return
        object_id = str(data.get('id') or '')
        if not valid_name(object_id):
            return
        op = data.get('op')
        ActivityLog().info(f'[avatar] Avatar {object_id} {"replaced" if op == "put" else "deleted"} on {self.device_label(data.get("device_id"))}')
        if op == 'put':
            with self.lock:
                self.missing.discard((account.id, object_id))
            cached = self.cache.get(account.id, object_id)
            if cached is not None and cached[0] == data.get('etag'):
                return
            # fetched even when the contact has not reached this device yet: it is about
            # to, and an orphan is dropped by the next sync
            self.fetch(account, object_id, cached_etag=cached[0] if cached else None, force=True,
                       meta={'device_id': data.get('device_id'), 'modified': data.get('modified')})
        elif op == 'delete':
            with self.lock:
                self.missing.add((account.id, object_id))
            self.cache.drop(account.id, object_id)
            self._post_changed([object_id])

    # avatars: writing

    @staticmethod
    def _default_uri(contact):
        try:
            uri = contact.uris.default or next(iter(contact.uris))
        except (StopIteration, AttributeError):
            return ''
        return str(getattr(uri, 'uri', None) or '')

    @classmethod
    def describe_contact(cls, contact):
        """'Name <uri> (id)' for the log."""
        uri = cls._default_uri(contact)
        name = getattr(contact, 'name', None) or ''
        return ' '.join(part for part in (name, f'<{uri}>' if uri else '', f'({contact.id})') if part)

    @run_in_gui_thread
    def upload_avatar(self, contact, reason='retry'):
        """Upload a contact's picture (its user-set file), stored under the contact's id.

        The caller has saved the picture locally and set contact.avatar_pending, so it
        shows at once and survives a failed upload; the flag is cleared once the server
        has the object.
        """
        account = storage_account()
        if account is None or not valid_name(contact.id):
            return False
        who = self.describe_contact(contact)
        path = user_picture_path(contact.id)
        data = encode_avatar(path) if os.path.isfile(path) else None
        if not data:
            ActivityLog().error(f'[avatar] Cannot encode the picture of {who} ({reason})')
            if not os.path.isfile(path):
                contact.avatar_pending = False      # nothing left to upload
                contact.save()
            return False
        with self.lock:
            if contact.id in self.uploading:
                return False
            self.uploading.add(contact.id)
        self._upload(account, contact, data, who, reason)
        return True

    @run_in_thread('account-storage')
    def _upload(self, account, contact, data, who, reason):
        try:
            response = self._request(account, 'PUT', f'{AVATARS_NAMESPACE}/{contact.id}', body=data,
                                     headers={'Content-Type': AVATAR_CONTENT_TYPE, 'X-Sylk-Device-Id': self.device_id()})
            if response.status in (200, 201):
                body = response.json() or {}
                etag = body.get('etag') or response.etag
                self.cache.put(account.id, contact.id, etag, AVATAR_CONTENT_TYPE, data,
                               {'device_id': self.device_id(), 'modified': body.get('modified') or int(time.time() * 1000)})
                with self.lock:
                    self.missing.discard((account.id, contact.id))
                ActivityLog().info(f'[avatar] Uploaded the picture of {who}: {reason}, {len(data)} bytes, etag {etag}')
            self._uploaded(contact, response.status, who, reason)
        finally:
            with self.lock:
                self.uploading.discard(contact.id)

    @run_in_gui_thread
    def _uploaded(self, contact, status, who, reason):
        if contact.id not in {item.id for item in self._contacts()}:
            return
        if status in (200, 201):
            # the stored copy is the picture now
            contact.avatar_pending = False
            contact.alternate_icon = None
            IconManager().remove(contact.id + '_alt')
            contact.save()
            self._post_changed([contact.id])
        elif status in (413, 507):
            ActivityLog().info(f'[avatar] Picture of {who} not uploaded ({reason}): ' + ('too large (413)' if status == 413 else 'account storage full (507)'))
            # keep the old avatar (the stored one, else the presence icon): the rejected picture goes,
            # or the next sync would push it again
            contact.avatar_pending = False
            IconManager().remove(contact.id + '_alt')
            contact.alternate_icon = None
            contact.save()
            self._post_changed([contact.id])
            title = translate('account_storage', 'Contact picture not saved')
            if status == 413:
                body = translate('account_storage', 'The picture is too large for the server')
            else:
                body = translate('account_storage', 'The account storage on the server is full')
            tell_user(title, body, contact.name or None)
        else:
            # network error, 401 after the retry, 5xx: kept locally and pending, retried later
            ActivityLog().info(f'[avatar] Upload of the picture of {who} postponed ({reason}): HTTP {status or "network error"}, retried later')

    @run_in_gui_thread
    def retry_pending(self):
        if storage_account() is None:
            return
        for contact in self._contacts():
            if getattr(contact, 'avatar_pending', False) and contact.id not in self.uploading:
                self.upload_avatar(contact)
            elif getattr(contact, 'avatar_delete_pending', False):
                self.remove_avatar(contact)

    @run_in_gui_thread
    def remove_avatar(self, contact):
        """Delete a contact's stored picture.

        contact is a Contact, or the id of one that no longer exists (deleted
        permanently: a failed delete is then left to the garbage collection). For a live
        contact a failed delete stays in avatar_delete_pending and is retried.
        """
        contact_id = contact if isinstance(contact, str) else contact.id
        account = storage_account()
        if account is None or not valid_name(contact_id):
            return
        self.cache.drop(account.id, contact_id)        # off the screen now, whatever the server says
        self._post_changed([contact_id])
        self._delete(account, contact_id, None if isinstance(contact, str) else contact)

    @run_in_thread('account-storage')
    def _delete(self, account, object_id, contact):
        response = self._request(account, 'DELETE', f'{AVATARS_NAMESPACE}/{object_id}', headers={'X-Sylk-Device-Id': self.device_id()})
        ok = response.status in (204, 404)
        if ok:
            ActivityLog().info(f'[avatar] Avatar of {object_id} deleted')
        else:
            ActivityLog().info(f'[avatar] Cannot delete avatar {object_id}: HTTP {response.status or "error"} '
                               f'({"retried later" if contact is not None else "left to the garbage collection"})')
        self.cache.drop(account.id, object_id)
        if contact is not None:
            self._deleted(contact, ok)

    @run_in_gui_thread
    def _deleted(self, contact, ok):
        if contact.id not in {item.id for item in self._contacts()}:
            return
        if bool(getattr(contact, 'avatar_delete_pending', False)) == (not ok):
            return
        contact.avatar_delete_pending = not ok
        contact.save()


def tell_user(title, body, subject=None):
    """A desktop notification (org.freedesktop.Notifications), else a message box that does not block."""
    text = f'{subject}: {body}' if subject else body
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync('org.freedesktop.Notifications', '/org/freedesktop/Notifications', 'org.freedesktop.Notifications', 'Notify',
                      GLib.Variant('(susssasa{sv}i)', ('Blink', 0, '', title, text, [], {}, -1)), GLib.VariantType('(u)'), 0, 5000, None)
        return
    except Exception:
        pass
    from PyQt6.QtWidgets import QMessageBox
    box = QMessageBox(QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    box.setModal(False)
    box.show()
