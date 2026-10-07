"""Where the file of a file transfer message is on this computer, if it is.

Received files are kept as file_transfers/<account>/<peer>/<transfer id>/<name>
(blink.configuration.datatypes.sylk_file_path), older ones as
downloads/<transfer id>/<name>; an encrypted one may still have its .asc. The
transfer id is the envelope's transfer_id, else the URL's id segment, else the
message id: all are tried, under any peer folder of any account. A file being
sent (MessageItem.upload) is where it was sent from.
"""

import glob
import os

from blink.message_envelopes import file_transfer_envelope
from blink.resources import ApplicationData


__all__ = ['local_file', 'file_info', 'failure_reason', 'transfer_ids']


def _candidates(item):
    meta = file_transfer_envelope(item.content) if item.content else None
    if not meta or not meta.get('filename'):
        return None, []
    from blink.file_transfer import safe_component, transfer_filename
    name = transfer_filename(meta['filename'])
    ids = []
    for transfer_id in (meta.get('transfer_id'), _url_id(meta.get('url')), item.id):
        if transfer_id and transfer_id not in ids:
            ids.append(str(transfer_id))
    return name, [safe_component(transfer_id) for transfer_id in ids]


def _url_id(url):
    # <base>/<sender>/<receiver>/<transfer id>/<name>
    parts = str(url or '').split('?', 1)[0].rstrip('/').split('/')
    return parts[-2] if len(parts) >= 5 else None


def transfer_ids(item):
    """Every id a file transfer message may be known by: its envelope's transfer_id, its URL's id, its message id."""
    meta = file_transfer_envelope(item.content) if item.content else None
    ids = {str(item.id)}
    if meta:
        ids.update(str(transfer_id) for transfer_id in (meta.get('transfer_id'), _url_id(meta.get('url'))) if transfer_id)
    return ids


def file_info(item):
    """{'name', 'size', 'type'} of a file transfer message (from its envelope), or None."""
    meta = file_transfer_envelope(item.content) if item.content else None
    if not meta or not meta.get('filename'):
        return None
    from blink.file_transfer import transfer_filename
    name = transfer_filename(meta['filename'])
    if name.lower().endswith('.asc'):
        name = name[:-4]
    try:
        size = int(meta.get('filesize') or 0) or None
    except (TypeError, ValueError):
        size = None
    return {'name': name, 'size': size, 'type': str(meta.get('filetype') or '')}


def failure_reason(item):
    """Why fetching the file failed, when it did and nothing has replaced the failure (.failure.json), else None."""
    import json
    if getattr(item, 'upload', None) is not None:
        return None
    _, ids = _candidates(item)
    for transfer_id in ids:
        for path in glob.glob(os.path.join(ApplicationData.get('file_transfers'), '*', '*', glob.escape(transfer_id), '.failure.json')) + \
                [os.path.join(ApplicationData.get('downloads'), transfer_id, '.failure.json')]:
            try:
                with open(path) as failure:
                    return json.load(failure).get('reason') or 'failed'
            except (OSError, ValueError):
                continue
    return None


def local_file(item):
    """The path of the message's file, or None when it is not a file or not downloaded."""
    if item.category not in ('image', 'audio', 'video', 'other'):
        return None
    if getattr(item, 'upload', None) is not None:
        path = item.upload['path']
        return path if os.path.isfile(path) else None
    name, ids = _candidates(item)
    if not name:
        return None
    names = [name, name[:-4]] if name.endswith('.asc') else [name]
    roots = [os.path.join(ApplicationData.get('file_transfers'), glob.escape(item.account_id or '*') if item.account_id else '*', '*'),
             os.path.join(ApplicationData.get('file_transfers'), '*', '*')]
    for transfer_id in ids:
        for root in roots:
            for filename in names:
                for path in glob.glob(os.path.join(root, glob.escape(transfer_id), glob.escape(filename))):
                    if os.path.isfile(path) and not path.endswith('.asc'):
                        return path
        for filename in names:
            path = os.path.join(ApplicationData.get('downloads'), transfer_id, filename)
            if os.path.isfile(path) and not path.endswith('.asc'):
                return path
    return None
