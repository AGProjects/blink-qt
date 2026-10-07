"""Where the file of a file transfer message is on this computer, if it is.

Received files are kept as file_transfers/<account>/<peer>/<transfer id>/<name>
(blink.configuration.datatypes.sylk_file_path), older ones as
downloads/<transfer id>/<name>; an encrypted one may still have its .asc. The
transfer id is the envelope's transfer_id, else the URL's id segment, else the
message id: all are tried, under any peer folder of any account.
"""

import glob
import os

from blink.message_envelopes import file_transfer_envelope
from blink.resources import ApplicationData


__all__ = ['local_file']


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


def local_file(item):
    """The path of the message's file, or None when it is not a file or not downloaded."""
    if item.category not in ('image', 'audio', 'video', 'other'):
        return None
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
