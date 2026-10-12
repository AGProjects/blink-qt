"""Data export and import between devices of one account: the protocol.

Importing history from a Sylk Mobile data export (or from another Blink), and
the crypto the export side uses too (blink.data_export).

The phone runs a small HTTP server on the LAN and announces it to its own
account as application/sylk-data-export, PGP-encrypted to the account's own
public key. The announcement forks to every device of the account; this is
the receiving half. Wire format and API: sylk-mobile
docs/export/Readme.md, app/ExportAnnounce.js, app/ExportCrypto.js,
app/ImportClient.js.

  payload   {v, server, key, enc, timestamp}
  server    http://ip:port of the phone's export server
  key       the one-time auth token (sent as ?token=)
  enc       base64 32-byte NaCl secretbox key. With it, every response we
            ask for with X-Sylk-Enc: 1 comes back as nonce(24) || box.
  timestamp unix seconds; only acted on when fresh (60s, 10s skew)

Import is ADD-ONLY, exactly as on the phone: the unique id of a message or a
file is its msg_id, a row that already exists here is never touched, and a
file row that exists but whose bytes are missing gets its bytes and nothing
else. Contacts are not imported -- they converge through XCAP.

No Qt here, so the format, the crypto and the row mapping can be tested
alone.
"""

import base64
import datetime
import json
import struct
import threading
import time
import urllib.parse
import urllib.request


EXPORT_CONTENT_TYPE = 'application/sylk-data-export'
FRESH_WINDOW_SECONDS = 60
CLOCK_SKEW_SECONDS = 10

FILE_TRANSFER_TYPE = 'application/sylk-file-transfer'

KINDS = ('messages', 'files')
MESSAGE_CATEGORIES = (('all', 'All'), ('text', 'Text'), ('links', 'Links'), ('location', 'Location'))
FILE_CATEGORIES = (('all', 'All'), ('image', 'Images'), ('audio', 'Audio'), ('video', 'Video'), ('other', 'Other'))

HTTP_TIMEOUT = 20.0
BLOB_TIMEOUT = 300.0
PING_TIMEOUT = 8.0

# A request that fails on the way (refused, reset, timed out, a 5xx) is tried
# again after each of these pauses before it is given up. The phone's server
# is one JS thread behind react-native-tcp-socket: while it reads and seals a
# large file it answers nobody else, and a Wi-Fi radio going to sleep drops a
# connection or two. Neither is the export having stopped.
RETRY_DELAYS = (1.0, 3.0, 6.0)


# -- the announcement ---------------------------------------------------------

def parse_announcement(text):
    """The announcement as a dict, or None if this is not one."""
    if isinstance(text, bytes):
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError:
            return None
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if not obj.get('server') or not obj.get('key') or not isinstance(obj.get('timestamp'), (int, float)):
        return None
    return {'v': obj.get('v') or 1,
            'server': str(obj['server']),
            'key': str(obj['key']),
            'enc': str(obj['enc']) if obj.get('enc') else '',
            'timestamp': obj['timestamp']}


def is_fresh(timestamp, now=None, window=FRESH_WINDOW_SECONDS):
    """Created within the window. A journal replay of an old export is not."""
    if not isinstance(timestamp, (int, float)):
        return False
    age = (time.time() if now is None else now) - timestamp
    return -CLOCK_SKEW_SECONDS <= age <= window


# -- NaCl secretbox (XSalsa20-Poly1305) -------------------------------------
#
# PyNaCl (python3-nacl). Without it: pycryptodome's Salsa20 for the stream,
# cryptography's Poly1305 for the tag, and HSalsa20 -- one core, run once per
# message -- in Python.

_SIGMA = (0x61707865, 0x3320646e, 0x79622d32, 0x6b206574)
_MASK = 0xffffffff


def _rotl(v, n):
    return ((v << n) & _MASK) | (v >> (32 - n))


def _hsalsa20(key, nonce16):
    k = struct.unpack('<8I', key)
    n = struct.unpack('<4I', nonce16)
    x = [_SIGMA[0], k[0], k[1], k[2], k[3], _SIGMA[1], n[0], n[1],
         n[2], n[3], _SIGMA[2], k[4], k[5], k[6], k[7], _SIGMA[3]]

    def qr(a, b, c, d):
        x[b] ^= _rotl((x[a] + x[d]) & _MASK, 7)
        x[c] ^= _rotl((x[b] + x[a]) & _MASK, 9)
        x[d] ^= _rotl((x[c] + x[b]) & _MASK, 13)
        x[a] ^= _rotl((x[d] + x[c]) & _MASK, 18)

    for _ in range(10):
        qr(0, 4, 8, 12); qr(5, 9, 13, 1); qr(10, 14, 2, 6); qr(15, 3, 7, 11)
        qr(0, 1, 2, 3); qr(5, 6, 7, 4); qr(10, 11, 8, 9); qr(15, 12, 13, 14)
    return struct.pack('<8I', x[0], x[5], x[10], x[15], x[6], x[7], x[8], x[9])


def _poly1305_tag(key, data):
    try:
        from cryptography.hazmat.primitives.poly1305 import Poly1305
        return Poly1305.generate_tag(key, data)
    except Exception:
        pass            # not built, or an OpenSSL without it
    r = int.from_bytes(key[:16], 'little') & 0x0ffffffc0ffffffc0ffffffc0fffffff
    s = int.from_bytes(key[16:], 'little')
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(data), 16):
        block = data[i:i + 16] + b'\x01'
        acc = ((acc + int.from_bytes(block, 'little')) * r) % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, 'little')


class DecryptionError(Exception):
    pass


def secretbox_open(key_b64, data):
    """nonce(24) || box -> plaintext. Raises DecryptionError on a bad tag."""
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise DecryptionError('bad key length')
    data = bytes(data)
    if len(data) < 24 + 16:
        raise DecryptionError('truncated')
    nonce, box = data[:24], data[24:]
    try:
        import nacl.secret
        import nacl.exceptions
    except ImportError:
        pass
    else:
        try:
            return nacl.secret.SecretBox(key).decrypt(box, nonce)
        except nacl.exceptions.CryptoError as e:
            raise DecryptionError(str(e))

    from Crypto.Cipher import Salsa20
    import hmac
    subkey = _hsalsa20(key, nonce[:16])
    tag, ciphertext = box[:16], box[16:]
    stream = Salsa20.new(key=subkey, nonce=nonce[16:]).encrypt(bytes(32) + ciphertext)
    if not hmac.compare_digest(_poly1305_tag(stream[:32], ciphertext), tag):
        raise DecryptionError('decryption failed (wrong key or corrupted data)')
    return stream[32:]



def secretbox_seal(key_b64, plaintext, nonce=None):
    """plaintext -> nonce(24) || box, as the phone's ExportCrypto.encrypt makes it (for Blink's export)."""
    import os as _os
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise DecryptionError('bad key length')
    nonce = nonce or _os.urandom(24)
    plaintext = bytes(plaintext)
    try:
        import nacl.secret
    except ImportError:
        pass
    else:
        return nonce + bytes(nacl.secret.SecretBox(key).encrypt(plaintext, nonce).ciphertext)
    from Crypto.Cipher import Salsa20
    subkey = _hsalsa20(key, nonce[:16])
    stream = Salsa20.new(key=subkey, nonce=nonce[16:]).encrypt(bytes(32) + plaintext)
    ciphertext = stream[32:]
    return nonce + _poly1305_tag(stream[:32], ciphertext) + ciphertext


def new_secretbox_key():
    import os as _os
    return base64.b64encode(_os.urandom(32)).decode('ascii')

# -- talking to the phone -----------------------------------------------------

class ExportServerError(Exception):
    pass


class _Attempt(Exception):
    def __init__(self, error, retryable):
        Exception.__init__(self, str(error))
        self.error = error
        self.retryable = retryable


class ImportClient(object):
    """The phone's export API, as the importing phone uses it."""

    def __init__(self, server, token, enc_key=''):
        self.server = str(server or '').rstrip('/')
        self.token = token or ''
        self.enc_key = enc_key or ''
        # Called as on_retry(path, attempt, attempts, reason) before a retry.
        self.on_retry = None
        # Checked between retries; True abandons the request at once.
        self.should_stop = None
        # Liveness, for the heartbeat: a request in flight or one that just
        # answered says more about the phone than a ping could.
        self.last_success = 0.0
        # File transfer ids the other device holds no bytes for: said by a Blink exporter
        # (without_file, file: false) or learned from a 404 on /api/blob (the phone)
        self.no_file = set()
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def busy(self):
        return self._in_flight > 0

    def url(self, path, params=None):
        query = dict((k, v) for k, v in (params or {}).items() if v is not None and v != '')
        if self.token:
            query['token'] = self.token
        return self.server + path + ('?' + urllib.parse.urlencode(query) if query else '')

    def _fetch(self, path, params, timeout):
        """One attempt: (body, sealed), or raises _Attempt(error, retryable)."""
        request = urllib.request.Request(self.url(path, params))
        if self.enc_key:
            request.add_header('X-Sylk-Enc', '1')
        with self._lock:
            self._in_flight += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                sealed = bool(response.headers.get('X-Sylk-Enc'))
            self.last_success = time.monotonic()
            return body, sealed
        except urllib.error.HTTPError as e:
            # It answered: alive. Only a server-side failure is worth asking
            # again; a 401 or a 404 will say the same thing next time.
            self.last_success = time.monotonic()
            raise _Attempt(ExportServerError('HTTP %d for %s' % (e.code, path)), e.code >= 500)
        except Exception as e:
            # Refused, reset, timed out, cut off half way (IncompleteRead).
            raise _Attempt(ExportServerError('%s: %s' % (path, getattr(e, 'reason', None) or e)), True)
        finally:
            with self._lock:
                self._in_flight -= 1

    def _get(self, path, params=None, timeout=HTTP_TIMEOUT):
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                body, sealed = self._fetch(path, params, timeout)
                break
            except _Attempt as failed:
                if not failed.retryable or attempt == attempts:
                    raise failed.error
                if self.should_stop is not None and self.should_stop():
                    raise failed.error
                if self.on_retry is not None:
                    try:
                        self.on_retry(path, attempt, attempts, str(failed.error))
                    except Exception:
                        pass
                time.sleep(RETRY_DELAYS[attempt - 1])
        if sealed:
            if not self.enc_key:
                raise ExportServerError('%s came back encrypted and we hold no key' % path)
            try:
                body = secretbox_open(self.enc_key, body)
            except DecryptionError as e:
                raise ExportServerError('%s: %s' % (path, e))
        return body

    def _json(self, path, params=None):
        body = self._get(path, params)
        try:
            return json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as e:
            raise ExportServerError('%s: not JSON (%s)' % (path, e))

    def ping(self):
        try:
            with urllib.request.urlopen(self.server + '/api/ping', timeout=PING_TIMEOUT):
                pass
        except urllib.error.HTTPError:
            pass                    # any answer means it is still there
        except Exception:
            return False
        self.last_success = time.monotonic()
        return True

    def summary(self):
        return self._json('/api/summary')

    def calendar(self, contact=None):
        return (self._json('/api/calendar', {'contact': contact}) or {}).get('index') or {}

    def id_index(self, kind, category):
        items = (self._json('/api/idindex', {'kind': kind, 'category': category}) or {}).get('items') or []
        self.no_file.update(item['id'] for item in items if item.get('id') and item.get('file') is False)
        return items

    def ids(self, kind, category, period, contact=None):
        reply = self._json('/api/ids', {'kind': kind, 'category': category, 'period': period, 'contact': contact}) or {}
        self.no_file.update(reply.get('without_file') or ())
        return reply.get('ids') or []

    def rows_bulk(self, kind, category, period, contact=None):
        return (self._json('/api/rows-bulk', {'kind': kind, 'category': category,
                                              'period': period, 'contact': contact}) or {}).get('rows') or []

    def row(self, msg_id):
        return self._json('/api/meta', {'id': msg_id})

    def blob(self, msg_id):
        if msg_id in self.no_file:
            raise ExportServerError('not on the other device')
        try:
            return self._get('/api/blob', {'id': msg_id}, timeout=BLOB_TIMEOUT)
        except ExportServerError as e:
            if str(e).startswith('HTTP 404 '):
                self.no_file.add(msg_id)
            raise


# -- the calendar ---------------------------------------------------------------

def day_list(index, kind, category):
    """[(day, count)] newest first, for the kind and category selected.

    The phone's calendar index is per category; 'All' is the sum of the
    categories that make up the kind, as the phone's own import merges them.
    """
    if kind == 'files':
        keys = ['image', 'audio', 'video', 'other'] if category == 'all' else [category]
    else:
        keys = ['text', 'location'] if category == 'all' else [category]
    days = {}
    for key in keys:
        for entry in index.get(key) or []:
            day = entry.get('day')
            if day:
                days[day] = days.get(day, 0) + int(entry.get('count') or 0)
    return sorted(days.items(), reverse=True)


def aggregate(days, length, prefix=None):
    """Counts summed over day[:length], optionally inside one prefix."""
    result = {}
    for day, count in days:
        if prefix and not day.startswith(prefix):
            continue
        key = day[:length]
        result[key] = result.get(key, 0) + count
    return sorted(result.items(), reverse=True)


# -- mapping a phone row to what the journal hands us ----------------------------

def other_party(row, account_id):
    if row.get('from_uri') == account_id:
        return row.get('to_uri') or ''
    return row.get('from_uri') or ''


def _json_dict(value):
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def transfer_envelope(row):
    """The file-transfer envelope Blink stores as the body of the row.

    The phone keeps the wire envelope in `content` and a richer copy in
    `metadata`; either will do. Its own paths (local_url and friends) mean
    nothing here and are dropped.
    """
    meta = _json_dict(row.get('content'))
    if not meta or not meta.get('filename'):
        meta = _json_dict(row.get('metadata')) or {}
    extra = _json_dict(row.get('metadata')) or {}
    for key in ('filename', 'filetype', 'filesize', 'url', 'transfer_id', 'until'):
        if not meta.get(key) and extra.get(key):
            meta[key] = extra[key]
    for key in list(meta):
        if key.startswith('local') or key in ('path', 'progress', 'b64', 'decrypted'):
            meta.pop(key, None)
    if not meta.get('transfer_id'):
        meta['transfer_id'] = row.get('related_msg_id') or row.get('msg_id')
    if not meta.get('filename'):
        meta['filename'] = str(meta['transfer_id'])
    return meta


def _iso(row):
    ts = row.get('unix_timestamp')
    try:
        ts = float(ts)
        if ts > 1e11:           # milliseconds
            ts /= 1000.0
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return str(row.get('timestamp') or '') or datetime.datetime.now(datetime.timezone.utc).isoformat()


def _status(row, direction):
    state = str(row.get('state') or '').lower()
    if direction == 'incoming':
        return 'displayed'          # history: it was read where it was kept
    if state in ('failed', 'error'):
        return 'failed'
    if state == 'displayed' or str(row.get('received')) == '2':
        return 'displayed'
    if state in ('delivered', 'received') or row.get('received'):
        return 'delivered'
    return 'sent'


def journal_entry(row, account_id):
    """(msg, direction, status, encryption, cpim_from, cpim_to) for a row.

    `msg` is shaped like a journal entry, so the row goes into history by
    the same road a replicated message does.
    """
    contact = other_party(row, account_id)
    direction = row.get('direction')
    if direction not in ('incoming', 'outgoing'):
        direction = 'outgoing' if row.get('from_uri') == account_id else 'incoming'
    content_type = row.get('content_type') or 'text/plain'
    if content_type == FILE_TRANSFER_TYPE:
        content = json.dumps(transfer_envelope(row))
    else:
        content = row.get('content')
        content = '' if content is None else str(content)
    stripped = content.strip()
    if stripped.startswith('-----BEGIN PGP MESSAGE-----') and stripped.endswith('-----END PGP MESSAGE-----'):
        encryption = 'pgp_encrypted'
    else:
        encryption = ''
    msg = {'message_id': row.get('msg_id'),
           'content': content,
           'content_type': content_type,
           'contact': contact,
           'timestamp': _iso(row),
           'direction': direction,
           'disposition': [],
           'metadata': row.get('metadata') if content_type != FILE_TRANSFER_TYPE else None,
           'state': row.get('state')}
    if direction == 'incoming':
        cpim_from, cpim_to = contact, account_id
    else:
        cpim_from, cpim_to = account_id, contact
    return msg, direction, _status(row, direction), encryption, cpim_from, cpim_to


def looks_like_pgp(payload, meta=None):
    head = bytes(payload[:40]).lstrip()
    if head.startswith(b'-----BEGIN PGP MESSAGE'):
        return True
    names = [str((meta or {}).get(k) or '') for k in ('filename', 'url')]
    # Binary OpenPGP: a public-key encrypted session key packet, old or new format.
    return any(n.endswith('.asc') for n in names) and bool(payload) and payload[0] in (0x84, 0x85, 0xc1)
