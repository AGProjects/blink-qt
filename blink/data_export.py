"""Exporting history to another device: Blink as the export server a Sylk
Mobile (or another Blink) imports from.

The other half of blink.data_import, speaking the phone's own export API so the
phone's Import screen works against Blink unchanged (sylk-mobile
docs/export/Readme.md, app/ExportRouter.js, app/ExportServer.js):

  - an HTTP server on the LAN, on a random port, IPv4;
  - a one-time 8-character token, accepted as ?token= or Authorization: Bearer;
  - a per-session 32-byte secretbox key; a client sending X-Sylk-Enc: 1 gets
    every body back sealed (nonce || box, X-Sylk-Enc: 1);
  - announced to the account's own devices as application/sylk-data-export,
    PGP-encrypted to the account's own public key (sent by MessageManager).

What is offered: the account's text, location and file transfer rows, read
once when the export starts (a snapshot: the server answers from memory and
never touches the database), shaped as rows of the phone's messages table.
Contacts are not exported -- they converge through XCAP -- so the contacts
kind is empty. There is no browser web page and no zip download: this serves
the import, not a browser.

A file transfer is offered with the bytes Blink has: the decrypted file in
its transfer folder (file_transfers/<account>/<peer>/<id>/, or downloads/<id>/). Its row therefore names the file without .asc and carries no
.asc URL, or the phone would take the plaintext for ciphertext.

No Qt here, so the server can be tested alone.
"""

import datetime
import json
import os
import re
import socket
import threading
import time
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from blink.data_import import EXPORT_CONTENT_TYPE, FILE_TRANSFER_TYPE, new_secretbox_key, secretbox_seal


__all__ = ['ACTIVE_TOKENS', 'EXPORT_CONTENT_TYPE', 'ExportSnapshot', 'ExportServer', 'TransferStorage', 'make_token', 'announcement', 'local_ipv4']


PORT_MIN = 49152
PORT_MAX = 65535
PORT_TRIES = 8
TOKEN_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'       # Crockford base32, as the phone

MESSAGE_CATEGORIES = ('text', 'location')
FILE_CATEGORIES = ('image', 'audio', 'video', 'other')
SUMMARY_CATEGORIES = ('text', 'links', 'location', 'image', 'audio', 'video', 'other')
FILE_READ_CHUNK = 256 * 1024

# tokens of the exports running here: the announcement of one comes back to this device too, and is ignored
ACTIVE_TOKENS = set()

_LINK = re.compile(r'(https?://|www\.)\S', re.IGNORECASE)


def make_token():
    raw = os.urandom(8)
    return ''.join(TOKEN_ALPHABET[b % len(TOKEN_ALPHABET)] for b in raw)


def _norm_token(token):
    return re.sub(r'[^0-9A-Z]', '', str(token or '').upper())


def announcement(server, token, enc, timestamp=None):
    """The application/sylk-data-export payload (before PGP), as ExportAnnounce.buildAnnouncement."""
    return json.dumps({'v': 1, 'server': server, 'key': token, 'enc': enc,
                       'timestamp': int(time.time()) if timestamp is None else int(timestamp)})


def local_ipv4():
    """The IPv4 of the interface with the default route, or None. Nothing is sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('192.0.2.1', 9))         # TEST-NET-1: only picks the route
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith('0.') or address.startswith('127.') else address


def _bare(uri):
    text = str(uri or '').strip()
    for scheme in ('sips:', 'sip:'):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
    return text.split(';', 1)[0].lower()


def _display_name(meta):
    name = str(meta.get('filename') or '')
    return name[:-4] if name.endswith('.asc') else name


class TransferStorage(object):
    """Where received and sent files are kept, listed once: {transfer folder name: {file name: path}} of
    file_transfers/<account>/<peer>/<id>/ and downloads/<id>/, partial downloads left out."""

    def __init__(self, file_transfers_root, downloads_root):
        self.folders = {}
        for account in self._dirs(file_transfers_root):
            for peer in self._dirs(account):
                for transfer in self._dirs(peer):
                    self._add(transfer)
        for transfer in self._dirs(downloads_root):
            self._add(transfer)

    @staticmethod
    def _dirs(path):
        try:
            with os.scandir(path) as entries:
                return [entry.path for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    def _add(self, folder):
        try:
            with os.scandir(folder) as entries:
                files = {entry.name: entry.path for entry in entries
                         if not entry.name.endswith('.download') and not entry.name.startswith('.') and entry.is_file()}
        except OSError:
            return
        self.folders.setdefault(os.path.basename(folder), {}).update(files)

    def find(self, meta, message_id, encrypted=False):
        """The (decrypted) file of a transfer, or None: under any of the ids it is known by -- the
        envelope's transfer_id, the URL's id segment, the message id -- as the message pane looks.
        With encrypted, a .asc never decrypted here will do when the plain file is not there:
        the importing side decrypts it (data_import.looks_like_pgp)."""
        from blink.file_transfer import safe_component, transfer_filename
        name = transfer_filename(meta.get('filename'))
        plain = name[:-4] if name.endswith('.asc') else name
        url_parts = str(meta.get('url') or '').split('?', 1)[0].rstrip('/').split('/')
        for transfer_id in (meta.get('transfer_id'), url_parts[-2] if len(url_parts) >= 5 else None, message_id):
            if not transfer_id:
                continue
            files = self.folders.get(safe_component(transfer_id), {})
            for candidate in (plain, plain + '.asc') if encrypted else (plain,):
                path = files.get(candidate)
                if path is None:
                    continue
                try:
                    if os.path.getsize(path) > 0:
                        return path
                except OSError:
                    continue
        return None


def _json_dict(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


class ExportSnapshot(object):
    """The rows offered, built once from the history.

    db_rows are MessageHistory.data_export_rows tuples; envelope(body) parses a file transfer
    envelope (blink.message_envelopes.file_transfer_envelope); storage is a TransferStorage."""

    def __init__(self, account_id, db_rows, envelope, storage):
        self.account_id = _bare(account_id)
        self.storage = storage
        self.messages = []      # [(contact, day, row)] oldest first
        self.files = []         # [(contact, day, row, path or None)]
        self.by_id = {}         # msg_id: (row, path or None)
        self.file_bytes = 0
        for db_row in db_rows:
            try:
                self._add(db_row, envelope)
            except Exception:
                continue        # one odd row does not stop the export

    def _add(self, db_row, envelope):
        (msgid, local_uri, remote_uri, unix_time, day, direction, content_type, category, related_msg_id,
         related_action, status, encryption, metadata, body) = db_row
        me = self.account_id
        peer = _bare(remote_uri)
        if not peer or not msgid:
            return
        outgoing = direction == 'outgoing'
        content_type = str(content_type or 'text/plain')
        body = '' if body is None else (body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body))
        unix_time = int(unix_time or 0)
        iso = datetime.datetime.fromtimestamp(unix_time, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        status = str(status or '')
        armored = body.lstrip().startswith('-----BEGIN PGP MESSAGE-----')
        row = {'account': me,
               'msg_id': str(msgid),
               'timestamp': json.dumps(iso),               # the phone keeps JSON.stringify(Date)
               'unix_timestamp': unix_time,
               'content': body,
               'content_type': content_type,
               'metadata': metadata if metadata else None,
               'from_uri': me if outgoing else peer,
               'to_uri': peer if outgoing else me,
               'direction': 'outgoing' if outgoing else 'incoming',
               'state': status or None,
               'sent': 1 if outgoing else 0,
               'received': 1 if (not outgoing or status in ('delivered', 'displayed')) else 0,
               'deleted': 0,
               'pending': 0,
               'related_msg_id': related_msg_id or None,
               'related_action': related_action or None,
               # 1: ciphertext the receiving device decrypts; 2: decrypted here; 0: never encrypted
               'encrypted': 1 if armored else (2 if encryption else 0),
               'category': category or None,
               'has_link': 0,
               'day_id': str(day or '')}
        path = None
        if content_type == FILE_TRANSFER_TYPE or content_type == 'application/vnd.gsma.rcs-ft-http+xml':
            meta = envelope(body) if body and not armored else None
            if not meta or not meta.get('filename'):
                return
            path = self.storage.find(meta, str(msgid), encrypted=True)
            category = category if category in FILE_CATEGORIES else 'other'
            plain = {'filename': _display_name(meta),
                     'filetype': meta.get('filetype') or None,
                     'filesize': os.path.getsize(path) if path else (meta.get('filesize') or None),
                     'transfer_id': meta.get('transfer_id') or str(msgid)}
            if meta.get('until'):
                plain['until'] = meta['until']
            url = str(meta.get('url') or '')
            if url and not url.endswith('.asc'):
                plain['url'] = url          # an .asc URL would make the phone take the file for ciphertext
            extra = _json_dict(metadata) or {}
            for key, value in extra.items():
                if key not in plain and not key.startswith('local') and key not in ('url', 'path', 'b64', 'error'):
                    plain[key] = value
            row.update({'content': json.dumps(plain), 'content_type': FILE_TRANSFER_TYPE, 'metadata': json.dumps(plain),
                        'category': category, 'encrypted': 0, 'url': plain.get('url')})
            try:
                self.file_bytes += int(plain['filesize'] or 0)
            except (TypeError, ValueError):
                pass
            self.files.append((peer, row['day_id'], row, path))
        else:
            if category not in MESSAGE_CATEGORIES:
                return
            if category == 'text' and not armored and _LINK.search(body):
                row['has_link'] = 1
            self.messages.append((peer, row['day_id'], row))
        self.by_id[row['msg_id']] = (row, path)

    # -- selections, as the phone's provider makes them ------------------------------

    @staticmethod
    def _in(day, contact, item_contact, period):
        return (not contact or item_contact == contact) and (not period or period == 'all' or day.startswith(period))

    def message_rows(self, category='all', period='all', contact=None):
        contact = _bare(contact) if contact else None
        result = []
        for peer, day, row in self.messages:
            if category == 'text' and row['category'] != 'text':
                continue
            if category == 'links' and not (row['category'] == 'text' and row['has_link']):
                continue
            if category == 'location' and row['category'] != 'location':
                continue
            if self._in(day, contact, peer, period):
                result.append((peer, day, row))
        return result

    def file_rows(self, category='all', period='all', contact=None):
        contact = _bare(contact) if contact else None
        return [(peer, day, row, path) for peer, day, row, path in self.files
                if (category in ('all', '', None) or row['category'] == category) and self._in(day, contact, peer, period)]

    def rows(self, kind, category, period, contact):
        if kind == 'files':
            return [(peer, day, row) for peer, day, row, path in self.file_rows(category, period, contact)]
        if kind == 'messages':
            return self.message_rows(category, period, contact)
        return []

    def summary(self, device):
        messages = dict((category, 0) for category in SUMMARY_CATEGORIES)
        messages['total'] = 0
        for peer, day, row in self.messages:
            messages[row['category']] = messages.get(row['category'], 0) + 1
            messages['total'] += 1
        files = dict((category, {'count': 0, 'bytes': 0}) for category in FILE_CATEGORIES)
        files['total'] = {'count': 0, 'bytes': 0}
        for peer, day, row, path in self.files:
            size = int(json.loads(row['metadata']).get('filesize') or 0)
            for key in (row['category'], 'total'):
                files[key]['count'] += 1
                files[key]['bytes'] += size
            messages[row['category']] = messages.get(row['category'], 0) + 1
            messages['total'] += 1
        return {'device': device, 'account': self.account_id, 'generated_at': _now_iso(),
                'contacts': {'total': 0}, 'messages': messages, 'files': files}

    def calendar(self, contact=None):
        contact = _bare(contact) if contact else None
        counts = {}

        def bump(category, day):
            counts.setdefault(category, {})
            counts[category][day] = counts[category].get(day, 0) + 1
        for peer, day, row in self.messages:
            if day and (not contact or peer == contact):
                bump(row['category'], day)
                bump('all', day)
                if row['category'] == 'text' and row['has_link']:
                    bump('links', day)
        for peer, day, row, path in self.files:
            if day and (not contact or peer == contact):
                bump(row['category'], day)
                bump('all', day)
        index = dict((category, [{'day': day, 'count': days[day]} for day in sorted(days, reverse=True)])
                     for category, days in counts.items())
        return {'generated_at': _now_iso(), 'index': index}

    def contact_counts(self, kind, category, period):
        counts = {}
        for peer, day, row in self.rows(kind, category, period, None):
            counts[peer] = counts.get(peer, 0) + 1
        return [{'contact': contact, 'count': count} for contact, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


class _Handler(BaseHTTPRequestHandler):
    server_version = 'BlinkExport/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass        # the export logs requests itself

    def do_POST(self):
        self._reply_json(405, {'error': 'method not allowed'}, False)

    def do_GET(self):
        export = self.server.export
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        export._event(self.client_address[0], path)
        seal = bool(export.enc_key and self.headers.get('X-Sylk-Enc'))
        try:
            if path in ('/', '/index.html'):
                page = ('<!doctype html><meta charset="utf-8"><title>Blink export</title>'
                        '<p>This is Blink exporting its history. Open <b>Import</b> on your other device to copy it.</p>')
                return self._reply(200, 'text/html; charset=utf-8', page.encode('utf-8'), False)
            if path == '/api/ping':
                return self._reply(200, 'application/json', b'{"ok":true}', False)
            if not self._authorized(query):
                return self._reply_json(401, {'error': 'unauthorized'}, seal)
            snapshot = export.snapshot
            kind = query.get('kind') or 'messages'
            category = query.get('category') or 'all'
            period = query.get('period') or 'all'
            contact = query.get('contact') or None
            if path == '/api/summary':
                return self._reply_json(200, snapshot.summary(export.device), seal)
            if path == '/api/calendar':
                return self._reply_json(200, snapshot.calendar(contact), seal)
            if path == '/api/ids':
                ids = [row['msg_id'] for peer, day, row in snapshot.rows(kind, category, period, contact)]
                reply = {'kind': kind, 'count': len(ids), 'ids': ids}
                if kind == 'files':
                    # rows offered without bytes here (the row still carries the message); a Blink importer
                    # does not ask /api/blob for them, the phone ignores the key
                    reply['without_file'] = [message_id for message_id in ids if not snapshot.by_id[message_id][1]]
                return self._reply_json(200, reply, seal)
            if path == '/api/rows-bulk':
                rows = [row for peer, day, row in snapshot.rows(kind, category, period, contact)]
                return self._reply_json(200, {'count': len(rows), 'rows': rows}, seal)
            if path == '/api/idindex':
                items = [{'id': row['msg_id'], 'day': day, 'contact': peer} for peer, day, row in snapshot.rows(kind, category, 'all', contact)]
                if kind == 'files':
                    for item in items:
                        item['file'] = bool(snapshot.by_id[item['id']][1])
                return self._reply_json(200, {'items': items}, seal)
            if path == '/api/contacts-counts':
                contacts = snapshot.contact_counts(kind, category, period)
                return self._reply_json(200, {'count': len(contacts), 'contacts': contacts}, seal)
            if path == '/api/contacts.json':
                return self._reply_json(200, [], seal)
            if path == '/api/meta':
                found = snapshot.by_id.get(query.get('id') or '')
                if found is None:
                    return self._reply_json(404, {'error': 'transfer not found', 'id': query.get('id')}, seal)
                return self._reply_json(200, found[0], seal)
            if path == '/api/blob':
                return self._blob(snapshot, query.get('id') or '', seal)
            return self._reply_json(404, {'error': 'not found'}, seal)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            export._error('%s failed: %s' % (path, e))
            try:
                self._reply_json(500, {'error': 'internal'}, False)
            except Exception:
                pass

    def _authorized(self, query):
        token = self.server.export.token
        if query.get('token') and _norm_token(query['token']) == token:
            return True
        auth = self.headers.get('Authorization') or ''
        return auth.lower().startswith('bearer ') and _norm_token(auth[7:]) == token

    def _blob(self, snapshot, msg_id, seal):
        found = snapshot.by_id.get(msg_id)
        path = found[1] if found else None
        if not path or not os.path.isfile(path):
            return self._reply_json(404, {'error': 'file not found or not on device', 'id': msg_id}, seal)
        name = json.loads(found[0]['metadata']).get('filename') or 'file'
        if seal:
            with open(path, 'rb') as file:
                data = file.read()
            return self._reply(200, 'application/octet-stream', data, True)
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.send_header('Content-Disposition', 'attachment; filename="%s"' % name.replace('"', ''))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        with open(path, 'rb') as file:
            while True:
                chunk = file.read(FILE_READ_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _reply_json(self, status, obj, seal):
        return self._reply(status, 'application/json', json.dumps(obj).encode('utf-8'), seal)

    def _reply(self, status, content_type, body, seal):
        headers = {'Content-Type': content_type}
        if seal:
            body = secretbox_seal(self.server.export.enc_key, body)
            headers = {'Content-Type': 'application/octet-stream', 'X-Sylk-Enc': '1'}
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class ExportServer(object):
    """One export session: the server, its token and key, and the snapshot it serves.

    on_event(client, path) and on_error(text) are called from the server's threads."""

    def __init__(self, snapshot, device, host=None):
        self.snapshot = snapshot
        self.device = device
        self.token = make_token()
        self.enc_key = new_secretbox_key()
        self.host = host or local_ipv4()
        self.port = None
        self.requests = 0
        self.clients = set()
        self.last_event = None
        self.last_request_at = None
        self.on_event = None
        self.on_error = None
        self._httpd = None
        self._thread = None

    @property
    def url(self):
        return 'http://%s:%d' % (self.host, self.port) if self.host and self.port else None

    @property
    def running(self):
        return self._httpd is not None

    def start(self):
        import random
        last_error = None
        for attempt in range(PORT_TRIES):
            port = random.randint(PORT_MIN, PORT_MAX)
            try:
                httpd = ThreadingHTTPServer(('0.0.0.0', port), _Handler)
            except OSError as e:
                last_error = e
                continue
            httpd.daemon_threads = True
            httpd.export = self
            self._httpd = httpd
            self.port = port
            self._thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.5}, name='data-export-server', daemon=True)
            self._thread.start()
            ACTIVE_TOKENS.add(self.token)
            return self.url
        raise OSError('no free port for the export server: %s' % last_error)

    def stop(self):
        httpd, self._httpd = self._httpd, None
        ACTIVE_TOKENS.discard(self.token)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    def announcement(self):
        return announcement(self.url, self.token, self.enc_key)

    def _event(self, client, path):
        self.requests += 1
        self.clients.add(client)
        self.last_event = path
        self.last_request_at = time.monotonic()
        if self.on_event is not None:
            try:
                self.on_event(client, path)
            except Exception:
                pass

    def _error(self, text):
        if self.on_error is not None:
            try:
                self.on_error(text)
            except Exception:
                pass
