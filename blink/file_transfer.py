"""HTTP file transfers through SylkServer, as Sylk Mobile and Blink for macOS do them.

The URL of one transfer is

    <base>/<sender>/<receiver>/<transfer_id>/<filename>

where <base> is the server's API root followed by /filetransfer. The base is
learned from the first transfer that arrives (it came from the server, so it
is the most reliable description of the endpoint there is) and kept on the
account (sms.file_transfer_url); until then it is derived from the journal
URL, which is a guess about one path segment.

The helpers here are pure: the account handling is in blink.messages. Ported
from FileTransferCache.py of Blink for macOS.
"""

import os
import re

from urllib.parse import unquote


__all__ = ['FILE_TRANSFER_PATH', 'MAX_ENCRYPT_BYTES', 'upload_url', 'normalized_url', 'base_url_from_transfer', 'derive_base_url',
           'FAILURE_PERMANENT', 'FAILURE_TRANSIENT', 'FAILURE_GONE', 'GONE_STATUS', 'safe_component', 'transfer_filename',
           'transfer_folder', 'classify_download_failure']


# The path SylkServer serves file transfers from, appended to the API root.
FILE_TRANSFER_PATH = '/filetransfer'

# Files up to this size are PGP encrypted (.asc) to the other party and to us before upload, as on
# the other clients; larger ones go up as they are.
MAX_ENCRYPT_BYTES = 50 * 1000 * 1000


def upload_url(base, sender, receiver, transfer_id, filename):
    """Where one transfer lives: the URL to POST to and to send on."""
    return '%s/%s/%s/%s/%s' % (str(base).rstrip('/'), sender, receiver, transfer_id, filename)


# A URL that has been through quote() with its default safe set: the colons are
# escaped and the slashes are not, so https://host:9999/... arrives as https%3A//host%3A9999/...
_OVER_ENCODED = re.compile(r'^([A-Za-z][A-Za-z0-9+.\-]*)%3[Aa]//([^/]*)(.*)$')
_HAS_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*://')


def normalized_url(url):
    """A transfer URL with an over-encoded scheme and authority repaired.

    Some senders percent-encode the whole URL before putting it in the envelope.
    Only the scheme and the authority are repaired, and only when the URL does
    not already parse: a %3A inside the path is a character in a filename, and
    decoding it would ask the server for a different file.
    """
    text = str(url or '').strip()
    if not text or _HAS_SCHEME.match(text):
        return text
    match = _OVER_ENCODED.match(text)
    if match is None:
        return text
    scheme, authority, rest = match.groups()
    return '%s://%s%s' % (scheme, unquote(authority), rest)


def base_url_from_transfer(url):
    """The file transfer base behind a transfer URL, or None: the URL without its last four segments."""
    text = normalized_url(url).split('?')[0]
    if not text:
        return None
    parts = text.rsplit('/', 4)
    if len(parts) != 5 or not parts[0]:
        return None
    if not parts[0].endswith(FILE_TRANSFER_PATH):
        return None
    return parts[0]


def derive_base_url(history_url):
    """The file transfer base implied by the journal URL (<root>/messages/...), or None.

    A journal URL without /messages is shaped in a way this does not know, and
    inventing a path from it would be worse than waiting for the first transfer.
    """
    history_url = str(history_url or '')
    if not history_url:
        return None
    root = history_url.split('/messages')[0].rstrip('/')
    if root == history_url.rstrip('/'):
        return None
    return root + FILE_TRANSFER_PATH


# Downloads
#
# A received file lives in file_transfers/<account>/<peer>/<transfer id>/<name>, one folder per
# transfer, as on Blink for macOS: removing a message or a conversation removes its folders and
# nothing else. Every component comes off the wire, so each is made safe for a path.

def safe_component(text):
    """One path component: letters, digits and -_.@+ only, at most 96 characters, never empty or '..'."""
    keep = '-_.@+'
    safe = ''.join(c if (c.isalnum() or c in keep) else '_' for c in str(text or ''))[:96]
    return safe if safe.strip('.') else '_'


def transfer_filename(name):
    """The name a received file is stored under: its last path component, without leading dots."""
    name = str(name or '').replace('\\', '/').rsplit('/', 1)[-1].lstrip('.')
    return name or 'file'


def transfer_folder(root, account, peer, transfer_id):
    return os.path.join(root, safe_component(account), safe_component(peer), safe_component(transfer_id))


# Whether asking for the same transfer again could give a different answer. GONE: the server
# says the file is not there (404, 410), and it never will be again; PERMANENT: any other 4xx,
# or a body we hold a key for and cannot open; TRANSIENT: 5xx and network errors, worth a retry.
FAILURE_PERMANENT = 'permanent'
FAILURE_TRANSIENT = 'transient'
FAILURE_GONE = 'gone'
GONE_STATUS = (404, 410)


def classify_download_failure(status=None, error=None):
    """(reason, kind) for a failed download: an HTTP status, or a transport error."""
    if status in GONE_STATUS:
        return 'HTTP %d, the server does not have this file (expired, or stored under a different name)' % status, FAILURE_GONE
    if status and 400 <= status < 500:
        return 'HTTP %d' % status, FAILURE_PERMANENT
    if status:
        return 'HTTP %d' % status, FAILURE_TRANSIENT
    return str(error or 'unknown error'), FAILURE_TRANSIENT
