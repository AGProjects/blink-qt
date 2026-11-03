"""Disk usage of the file transfers, per contact: what the Contacts tab of the Data window drills into,
and removing the files chosen from this computer to free disk space.

Every folder of the account in Blink's own storage (file_transfers/<account>/<peer>/<transfer id>/, and
downloads/<transfer id>/ before that layout) is matched with the file transfer message it belongs to,
under any of the ids the message is known by (the envelope's transfer_id, the URL's id segment, the
message id), as blink.data_manage.build_file_rows does. A folder no message points to any more is listed
too, under its peer folder, as a leftover.

Removing a file keeps its message: the conversation still shows it, and while the server has it a click
downloads it again. The folder keeps a marker (REMOVED_MARKER) so that the message pane does not fetch it
again on its own; an explicit download removes the marker. A leftover's folder is removed whole.

The other devices of the account are told with one application/sylk-files-purge message carrying the
criteria (files_purge_payload); each removes the files that match on its own disk, the messages kept
(apply: files_purge_criteria, purge_matches). See docs/messaging/files-purge.md.

No Qt here, so the scan and the selection can be tested alone.
"""

import json
import os
import shutil
import uuid

from collections import Counter, namedtuple
from datetime import datetime, timedelta, timezone

from blink.file_transfer import FAILURE_GONE, REMOVED_MARKER, safe_component, transfer_filename
from blink.message_envelopes import file_transfer_envelope


__all__ = ['LocalRow', 'Folder', 'ON_SERVER_TOO', 'HERE_ONLY', 'LEFTOVER', 'AGES', 'scan_local_folders', 'build_local_rows',
           'select_local', 'size_by', 'total_size', 'remove_local_files', 'local_status_label', 'age_label',
           'FILES_PURGE_VERSION', 'files_purge_payload', 'files_purge_criteria', 'purge_matches', 'purge_label']


ON_SERVER_TOO = 'server'    # the server still has it: it can be downloaded again
HERE_ONLY = 'here'          # expired or gone from the server, never uploaded, or came over MSRP: removing loses it
LEFTOVER = 'leftover'       # no message points to its folder any more

STATUS_LABELS = {ON_SERVER_TOO: 'Also on the server', HERE_ONLY: 'Only here', LEFTOVER: 'Leftover (no message)'}

# (days, label) of the age filter: older than
AGES = ((None, 'Any age'), (7, 'Older than 1 week'), (30, 'Older than 1 month'), (90, 'Older than 3 months'), (180, 'Older than 6 months'),
        (365, 'Older than 1 year'), (730, 'Older than 2 years'))

# 'type', 'action' (the status), 'contact' and 'day' as the other Data window rows have them
LocalRow = namedtuple('LocalRow', 'id message_id contact day timestamp time direction type action name size files folder url until')

# one transfer folder: its path, [(file name, size)], newest modification time, the peer folder (None under downloads/)
Folder = namedtuple('Folder', 'path files mtime peer')

FILE_KINDS = ('image', 'audio', 'video', 'other')

_FILE_EXTENSIONS = {'image': ('jpg', 'jpeg', 'png', 'gif', 'heic', 'heif', 'webp', 'tiff', 'bmp'),
                    'video': ('mp4', 'mov', 'm4v', 'avi', 'mkv', 'webm', '3gp'),
                    'audio': ('mp3', 'm4a', 'wav', 'aac', 'ogg', 'opus', 'caf', 'flac')}


def local_status_label(status):
    if status is None:
        return 'Any'
    return STATUS_LABELS.get(status, status)


def age_label(days):
    return dict(AGES).get(days, f'Older than {days} days')


def _folder(path, peer):
    """The Folder at path, or None when it holds no file (markers and failures are not files)."""
    files = []
    mtime = 0.0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith('.'):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files.append((entry.name, stat.st_size))
                mtime = max(mtime, stat.st_mtime)
    except OSError:
        return None
    return Folder(path, tuple(sorted(files)), mtime, peer) if files else None


def _subfolders(path):
    try:
        with os.scandir(path) as entries:
            return [entry for entry in entries if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def scan_local_folders(file_transfers_root, downloads_root, account_id):
    """{transfer folder name: [Folder]} of the account's folders that hold files, and of downloads/."""
    folders = {}
    account_root = os.path.join(file_transfers_root, safe_component(account_id))
    for peer in _subfolders(account_root):
        for transfer in _subfolders(peer.path):
            folder = _folder(transfer.path, peer.name)
            if folder is not None:
                folders.setdefault(transfer.name, []).append(folder)
    if downloads_root:
        for transfer in _subfolders(downloads_root):
            folder = _folder(transfer.path, None)
            if folder is not None:
                folders.setdefault(transfer.name, []).append(folder)
    return folders


def _url_id(url):
    # <base>/<sender>/<receiver>/<transfer id>/<name>
    parts = str(url or '').split('?', 1)[0].rstrip('/').split('/')
    return parts[-2] if len(parts) >= 5 else None


def _parse_time(text):
    """A naive UTC timestamp as stored, aware; None when unreadable."""
    try:
        value = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _expired(until, now):
    if not until:
        return False
    try:
        from dateutil.parser import parse
        expires = parse(str(until))
    except (ValueError, OverflowError, TypeError, ImportError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < now


def _gone(folder):
    try:
        with open(os.path.join(folder.path, '.failure.json')) as file:
            failure = json.load(file)
    except (OSError, ValueError):
        return False
    return isinstance(failure, dict) and failure.get('kind') == FAILURE_GONE


def _file_kind(category, filetype, name):
    if category in FILE_KINDS:
        return category
    filetype = str(filetype or '').lower()
    for kind in ('image', 'video', 'audio'):
        if filetype.startswith(kind + '/'):
            return kind
    extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if extension == 'asc':
        return _file_kind(None, None, name[:-4])
    for kind, extensions in _FILE_EXTENSIONS.items():
        if extension in extensions:
            return kind
    return 'other'


def _main_file(folder):
    """The name the folder is known by: its largest file, the decrypted one before its .asc."""
    return max(folder.files, key=lambda item: (not item[0].endswith('.asc'), item[1]))[0]


def build_local_rows(db_rows, folders, now=None):
    """LocalRows from MessageHistory.data_file_transfers tuples and scan_local_folders: one per folder,
    the message it belongs to first, then the folders no message points to."""
    now = now or datetime.now(timezone.utc)
    used = set()
    rows = []
    for row_id, message_id, remote_uri, day, timestamp, direction, category, content in db_rows:
        meta = file_transfer_envelope(content) if content else None
        meta = meta if isinstance(meta, dict) else {}
        ids = []
        for transfer_id in (meta.get('transfer_id'), _url_id(meta.get('url')), message_id):
            if transfer_id and safe_component(transfer_id) not in ids:
                ids.append(safe_component(transfer_id))
        contact = str(remote_uri or '')
        peer = safe_component(contact) if contact else None
        found = None
        for transfer_id in ids:
            candidates = [folder for folder in folders.get(transfer_id, ()) if folder.path not in used]
            # the folder of this conversation first, then one under downloads/, then any
            candidates.sort(key=lambda folder: 0 if folder.peer == peer else 1 if folder.peer is None else 2)
            if candidates:
                found = candidates[0]
                break
        if found is None:
            continue
        used.add(found.path)
        name = transfer_filename(meta['filename']) if meta.get('filename') else _main_file(found)
        if name.endswith('.asc'):
            name = name[:-4]
        url = str(meta.get('url') or '')
        if url and not meta.get('msrp') and not _expired(meta.get('until'), now) and not _gone(found):
            status = ON_SERVER_TOO
        else:
            status = HERE_ONLY
        when = _parse_time(timestamp) or datetime.fromtimestamp(found.mtime, timezone.utc)
        rows.append(LocalRow(int(row_id), str(message_id) if message_id else None, contact, str(day or when.astimezone().date().isoformat()),
                             str(timestamp or ''), when, str(direction or ''), _file_kind(category, meta.get('filetype'), name), status, name,
                             sum(size for _, size in found.files), found.files, found.path, url, meta.get('until')))
    leftovers = [folder for entries in folders.values() for folder in entries if folder.path not in used and folder.peer is not None]
    for number, folder in enumerate(sorted(leftovers, key=lambda folder: folder.path), 1):
        when = datetime.fromtimestamp(folder.mtime, timezone.utc)
        name = _main_file(folder)
        rows.append(LocalRow(-number, None, folder.peer, when.astimezone().date().isoformat(), when.strftime('%Y-%m-%d %H:%M:%S'), when, '',
                             _file_kind(None, None, name), LEFTOVER, name[:-4] if name.endswith('.asc') else name,
                             sum(size for _, size in folder.files), folder.files, folder.path, '', None))
    return rows


def select_local(rows, kind=None, action=None, contact=None, older_than=None, now=None):
    """The rows of a type, a status, a contact and older than so many days; None is any."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than) if older_than else None
    return [row for row in rows
            if (kind is None or row.type == kind)
            and (action is None or row.action == action)
            and (contact is None or row.contact == contact)
            and (cutoff is None or row.time < cutoff)]


def total_size(rows):
    return sum(row.size for row in rows)


def size_by(rows, attribute):
    """[(value, bytes, files)] largest first, then by value."""
    sizes = Counter()
    counts = Counter()
    for row in rows:
        value = getattr(row, attribute)
        sizes[value] += row.size
        counts[value] += 1
    return sorted(((value, sizes[value], counts[value]) for value in sizes), key=lambda item: (-item[1], str(item[0])))


def _inside(path, roots):
    real = os.path.realpath(path)
    return any(real.startswith(root + os.sep) for root in roots)


def remove_local_files(rows, roots, stop=None, progress=None, now=None):
    """Remove the files of the rows from this computer, only inside Blink's own storage (roots). A
    message's folder keeps a marker, a leftover's folder goes whole. Returns (message ids whose file
    was removed, files removed, bytes freed, [(path, error)])."""
    roots = [os.path.realpath(root) for root in roots if root]
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    message_ids = []
    removed = freed = 0
    errors = []
    for done, row in enumerate(rows, 1):
        if stop is not None and stop():
            break
        if not _inside(row.folder, roots) or not os.path.isdir(row.folder) or os.path.islink(row.folder):
            errors.append((row.folder, 'not in the file transfers folder'))
            continue
        if row.action == LEFTOVER:
            try:
                shutil.rmtree(row.folder)
            except OSError as e:
                errors.append((row.folder, str(e)))
                continue
            removed += len(row.files)
            freed += row.size
            try:
                os.rmdir(os.path.dirname(row.folder))       # the peer folder, if this was its last transfer
            except OSError:
                pass
        else:
            failed = False
            for name, size in row.files:
                try:
                    os.unlink(os.path.join(row.folder, name))
                except FileNotFoundError:
                    continue
                except OSError as e:
                    errors.append((os.path.join(row.folder, name), str(e)))
                    failed = True
                    continue
                removed += 1
                freed += size
            try:
                with open(os.path.join(row.folder, REMOVED_MARKER), 'w') as marker:
                    json.dump({'removed': stamp, 'reason': 'removed to free disk space (Data manager)'}, marker)
            except OSError as e:
                errors.append((row.folder, str(e)))
            if row.message_id and not failed:
                message_ids.append(row.message_id)
        if progress is not None:
            progress(done, len(rows))
    return message_ids, removed, freed, errors


# Purging on the other devices
#
# One application/sylk-files-purge message (sent as application/sylk-api-files-purge from the account to
# itself; the server journals it and sends it to every device of the account) carries the criteria of a
# purge made here. A device removes the files of the file transfer messages that match, the messages kept;
# a device that does not know this content type stores it as an inert row and removes nothing.

FILES_PURGE_VERSION = 1


def _utc_iso(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{value.microsecond // 1000:03d}Z'


def files_purge_payload(kind, status, contact, older_than, device_id, purge_id=None, now=None):
    """The payload announcing a purge made with these filters, or None when nothing of it can be told:
    leftovers have no message another device could find them by. The age becomes an absolute time
    (`until`), so a device that receives it later removes the same files."""
    if status == LEFTOVER:
        return None
    now = now or datetime.now(timezone.utc)
    criteria = {'until': _utc_iso(now - timedelta(days=older_than) if older_than else now)}
    if kind is not None:
        criteria['category'] = kind
    if contact:
        criteria['contact'] = contact
    if status is not None:
        criteria['availability'] = status
    return {'v': FILES_PURGE_VERSION,
            'files_purge_id': purge_id or str(uuid.uuid4()),
            'device_id': device_id,
            'timestamp': _utc_iso(now),
            'criteria': criteria}


def files_purge_criteria(payload):
    """The criteria of a received purge, checked, or None (ignored): `until` is required, `category`
    is a file category, `availability` one of server or here; never a list of ids."""
    if not isinstance(payload, dict) or not isinstance(payload.get('criteria'), dict):
        return None
    criteria = payload['criteria']
    if 'ids' in criteria or not criteria.get('until'):
        return None
    until = _parse_time(str(criteria['until']).replace('Z', '+00:00'))
    if until is None:
        return None
    result = {'until': until}
    if criteria.get('category'):
        if criteria['category'] not in FILE_KINDS:
            return None
        result['category'] = criteria['category']
    if criteria.get('availability'):
        if criteria['availability'] not in (ON_SERVER_TOO, HERE_ONLY):
            return None
        result['availability'] = criteria['availability']
    if criteria.get('contact'):
        result['contact'] = str(criteria['contact']).strip()
    if criteria.get('from'):
        start = _parse_time(str(criteria['from']).replace('Z', '+00:00'))
        if start is None:
            return None
        result['from'] = start
    return result


def purge_matches(rows, criteria):
    """The LocalRows of messages a received purge selects (the contact was matched by the query, with
    every spelling of it): never a leftover, whose folder no message points to."""
    return [row for row in rows
            if row.action != LEFTOVER
            and row.time < criteria['until']
            and ('from' not in criteria or row.time >= criteria['from'])
            and ('category' not in criteria or row.type == criteria['category'])
            and ('availability' not in criteria or row.action == criteria['availability'])]


def purge_label(criteria):
    """The criteria as the Data manager would name them, for the log."""
    parts = [criteria.get('category') or 'all types', criteria.get('contact') or 'all contacts']
    if criteria.get('availability'):
        parts.append(local_status_label(criteria['availability']).lower())
    until = criteria['until']
    parts.append(f'before {until.isoformat() if hasattr(until, "isoformat") else until}')
    return ', '.join(parts)
