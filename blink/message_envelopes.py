"""Message content types, envelopes and categories shared with Blink for macOS and Sylk Mobile.

Ported from Blink for macOS MessageHost.py (file transfer, reply, label, peaks
and call recording envelopes, conversation preview, call detail records,
public_key_id) and
HistoryManager.py (classify_category); has_link follows Sylk Mobile
_hasLinkInText. Keep the function names and rules the same so fixes can be
carried between the clients by diffing.

Pure functions, no Qt. pgpy is imported only by public_key_id, sipsimple
only by this_device_id and the location layer only by classify_category.
"""

import json
import re
import unicodedata
import xml.etree.ElementTree as ElementTree

from html.entities import name2codepoint
from html.parser import HTMLParser


__all__ = ['TEXT_CONTENT_TYPES', 'PGP_PUBLIC_KEY_CONTENT_TYPE', 'PGP_PRIVATE_KEY_CONTENT_TYPE', 'KEY_CONTENT_TYPES',
           'FILE_TRANSFER_CONTENT_TYPE', 'RCS_FILE_TRANSFER_CONTENT_TYPE', 'FILE_TRANSFER_CONTENT_TYPES',
           'LOCATION_CONTENT_TYPE', 'METADATA_CONTENT_TYPE', 'CALL_CONTENT_TYPE', 'LEGACY_CALL_CONTENT_TYPE',
           'CONVERSATION_READ_CONTENT_TYPE', 'CONVERSATION_REMOVE_CONTENT_TYPE', 'MESSAGE_REMOVE_CONTENT_TYPE',
           'ADDRESSBOOK_UPDATE_CONTENT_TYPE', 'DATA_EXPORT_CONTENT_TYPE', 'CONTACT_UPDATE_CONTENT_TYPE',
           'API_TOKEN_CONTENT_TYPE', 'API_PGP_KEY_LOOKUP_CONTENT_TYPE', 'API_MESSAGE_REMOVE_CONTENT_TYPE',
           'API_CONVERSATION_READ_CONTENT_TYPE', 'API_CONVERSATION_REMOVE_CONTENT_TYPE',
           'MESSAGE_CATEGORIES', 'REPLY_ACTION', 'LABEL_ACTION', 'PEAKS_ACTION', 'CALL_RECORDING_ACTION',
           'html2txt', 'is_pgp_armoured', 'is_pure_emoji',
           'file_transfer_envelope', 'file_transfer_category', 'file_transfer_summary', 'recording_title',
           'transfer_error_note', 'merge_transfer_error',
           'quote_digest', 'conversation_preview',
           'reply_metadata', 'reply_envelope', 'label_metadata', 'label_envelope', 'peaks_metadata', 'peaks_envelope',
           'call_recording_metadata', 'call_recording_envelope',
           'CALL_RECORD_VERSION', 'CALL_SOURCE_RANK', 'MISSED_CALL_OUTCOMES', 'CALL_ATTENTION_OUTCOMES', 'SIP_STATUS_PHRASES',
           'sip_status_phrase', 'dominant_media', 'build_call_record', 'merge_call_records', 'this_device_id', 'call_answered_elsewhere',
           'call_record', 'format_call_duration', 'call_outcome', 'call_was_missed', 'call_needs_attention', 'call_lines', 'call_summary',
           'classify_category', 'has_link', 'public_key_id']


# Content types
#
TEXT_CONTENT_TYPES = ('text', 'text/plain', 'text/html')         # 'text' is the legacy spelling in old rows
PGP_PUBLIC_KEY_CONTENT_TYPE = 'text/pgp-public-key'
PGP_PRIVATE_KEY_CONTENT_TYPE = 'text/pgp-private-key'
KEY_CONTENT_TYPES = (PGP_PUBLIC_KEY_CONTENT_TYPE, PGP_PRIVATE_KEY_CONTENT_TYPE)

# Sylk sends a file transfer as a JSON envelope, GSMA RCS as XML; both are
# normalised to one dict by file_transfer_envelope.
FILE_TRANSFER_CONTENT_TYPE = 'application/sylk-file-transfer'
RCS_FILE_TRANSFER_CONTENT_TYPE = 'application/vnd.gsma.rcs-ft-http+xml'
FILE_TRANSFER_CONTENT_TYPES = (FILE_TRANSFER_CONTENT_TYPE, RCS_FILE_TRANSFER_CONTENT_TYPE)

LOCATION_CONTENT_TYPE = 'application/sylk-location-sharing'
# Sidecars keyed on another message or transfer: reply, label, peaks,
# call_recording, and the legacy location (read, never sent).
METADATA_CONTENT_TYPE = 'application/sylk-message-metadata'
# A call as a structured record (Blink only); the record rides in the metadata column.
CALL_CONTENT_TYPE = 'application/blink-call-detail-record'
# What Blink Qt wrote for calls before call detail records.
LEGACY_CALL_CONTENT_TYPE = 'application/blink-call-history'

# Received from the server (journal or live)
CONVERSATION_READ_CONTENT_TYPE = 'application/sylk-conversation-read'
CONVERSATION_REMOVE_CONTENT_TYPE = 'application/sylk-conversation-remove'
MESSAGE_REMOVE_CONTENT_TYPE = 'application/sylk-message-remove'     # also sent peer to peer over Bonjour
ADDRESSBOOK_UPDATE_CONTENT_TYPE = 'application/sylk-addressbook-update'
DATA_EXPORT_CONTENT_TYPE = 'application/sylk-data-export'
CONTACT_UPDATE_CONTENT_TYPE = 'application/sylk-contact-update'

# Requests to the server, sent without CPIM
API_TOKEN_CONTENT_TYPE = 'application/sylk-api-token'
API_PGP_KEY_LOOKUP_CONTENT_TYPE = 'application/sylk-api-pgp-key-lookup'
API_MESSAGE_REMOVE_CONTENT_TYPE = 'application/sylk-api-message-remove'
API_CONVERSATION_READ_CONTENT_TYPE = 'application/sylk-api-conversation-read'
API_CONVERSATION_REMOVE_CONTENT_TYPE = 'application/sylk-api-conversation-remove'


# The category filters, in the order and with the names Sylk Mobile uses
# (ReadyBox.categoryFilterItems). 'links' is a subset of 'text' (the has_link
# column), not a category of its own. 'call' is Blink's alone.
MESSAGE_CATEGORIES = (
    ('text', 'Text'),
    ('links', 'Links'),
    ('audio', 'Audio'),
    ('image', 'Image'),
    ('video', 'Video'),
    ('location', 'Locations'),
    ('call', 'Calls'),
    ('other', 'Other'),
)


# Text helpers
#
class _HTMLToText(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=False)
        self._buf = []
        self.hide_output = False

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'br') and not self.hide_output:
            self._buf.append('\n')
        elif tag in ('script', 'style'):
            self.hide_output = True

    def handle_startendtag(self, tag, attrs):
        if tag == 'br':
            self._buf.append('\n')

    def handle_endtag(self, tag):
        if tag in ('p', 'tr'):
            self._buf.append('\n')
        elif tag == 'td':
            self._buf.append('\t')
        elif tag in ('script', 'style'):
            self.hide_output = False

    def handle_data(self, text):
        if text and not self.hide_output:
            self._buf.append(re.sub(r'\s+', ' ', text))

    def handle_entityref(self, name):
        if name in name2codepoint and not self.hide_output:
            self._buf.append(chr(name2codepoint[name]))

    def handle_charref(self, name):
        if not self.hide_output:
            try:
                self._buf.append(chr(int(name[1:], 16) if name.lower().startswith('x') else int(name)))
            except (ValueError, OverflowError):
                pass

    def get_text(self):
        return re.sub(r' +', ' ', ''.join(self._buf))


def html2txt(html):
    """The plain text in a piece of HTML (entities resolved, scripts and styles dropped)."""
    parser = _HTMLToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.get_text()


def _text(body):
    if isinstance(body, bytes):
        try:
            return body.decode('utf-8')
        except UnicodeDecodeError:
            return None
    return body if isinstance(body, str) else None


def is_pgp_armoured(body):
    if not isinstance(body, str):
        return False
    stripped = body.strip()
    return stripped.startswith('-----BEGIN PGP MESSAGE-----') and stripped.endswith('-----END PGP MESSAGE-----')


def is_pure_emoji(text):
    """True for a body made of nothing but emoji (and joiners, modifiers, whitespace)."""
    seen = False
    for ch in text or '':
        if ch.isspace():
            continue
        cp = ord(ch)
        if cp in (0x200D, 0xFE0E, 0xFE0F, 0x20E3) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F:
            continue
        if cp < 0x80:
            return False
        if unicodedata.category(ch) == 'So' or 0x1F000 <= cp <= 0x1FAFF:
            seen = True
            continue
        return False
    return seen


# File transfers
#
#   {"filename", "filetype", "filesize", "transfer_id", "url", "until",
#    "sender": {"uri"}, "receiver": {"uri"}, "direction",
#    "duration"?, "encrypted"?, "call_recording"?, "error"?}
#
def _rcs_file_transfer_envelope(body):
    """A GSMA RCS FT-HTTP payload as the dict a Sylk envelope would give.

    The file is <file-info type="file">; a type="thumbnail" sibling is a
    smaller copy, used only when there is nothing else. Namespaces vary
    between implementations, so elements are matched on their local name.
    """
    body = _text(body)
    if body is None or 'file-info' not in body:
        return None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None

    def local(element):
        tag = element.tag if isinstance(element.tag, str) else ''
        return tag.rsplit('}', 1)[-1]

    chosen = thumbnail = None
    for info in root.iter():
        if local(info) != 'file-info':
            continue
        if (info.get('type') or '').lower() == 'thumbnail':
            thumbnail = thumbnail if thumbnail is not None else info
        elif chosen is None:
            chosen = info
    chosen = chosen if chosen is not None else thumbnail
    if chosen is None:
        return None

    meta = {}
    for child in chosen:
        name = local(child)
        if name == 'file-name':
            meta['filename'] = (child.text or '').strip()
        elif name == 'file-size':
            try:
                meta['filesize'] = int((child.text or '').strip())
            except ValueError:
                pass
        elif name == 'content-type':
            meta['filetype'] = (child.text or '').strip()
        elif name == 'data':
            meta['url'] = (child.get('url') or '').strip()
            if child.get('until'):
                meta['until'] = child.get('until')

    if not meta.get('url'):
        return None
    if not meta.get('filename'):
        tail = meta['url'].split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
        meta['filename'] = tail or 'file'
    return meta


def _parse_file_transfer_envelope(body):
    body = _text(body)
    if body is None:
        return None
    if body.lstrip().startswith('<'):
        return _rcs_file_transfer_envelope(body)
    if 'filename' not in body:
        return None
    try:
        meta = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict) or not meta.get('filename'):
        return None
    return meta


_envelope_cache = {}
_ENVELOPE_CACHE_MAX = 1024
_UNPARSED = object()


def file_transfer_envelope(body):
    """A file transfer's details, whichever wire format described it, or None.

    Memoised on the body: the same message is asked about several times (the
    caption, the category, the filter, the download) and a negative answer is
    worth caching most of all, every HTML message being a candidate XML parse.
    Each caller gets its own copy of the dict.
    """
    try:
        cached = _envelope_cache.get(body, _UNPARSED)
    except TypeError:
        cached = _UNPARSED
    if cached is not _UNPARSED:
        return dict(cached) if cached is not None else None
    meta = _parse_file_transfer_envelope(body)
    try:
        if len(_envelope_cache) >= _ENVELOPE_CACHE_MAX:
            _envelope_cache.clear()
        _envelope_cache[body] = meta
    except TypeError:
        pass
    return dict(meta) if meta is not None else None


_EXTENSION_CATEGORIES = {
    'jpg': 'image', 'jpeg': 'image', 'png': 'image', 'gif': 'image', 'heic': 'image', 'webp': 'image', 'tiff': 'image', 'bmp': 'image',
    'mp4': 'video', 'mov': 'video', 'm4v': 'video', 'avi': 'video', 'mkv': 'video', 'webm': 'video',
    'mp3': 'audio', 'm4a': 'audio', 'wav': 'audio', 'aac': 'audio', 'ogg': 'audio', 'opus': 'audio', 'caf': 'audio',
}


def file_transfer_category(body):
    """'image' / 'video' / 'audio' / 'other' for a file transfer, else None.

    A known extension wins over the mime type (an older recorder sent voice
    notes as video/mp4); call_recording means audio unless the extension says
    video; then the mime prefix; then 'other'.
    """
    meta = file_transfer_envelope(body)
    if meta is None:
        return None
    filetype = str(meta.get('filetype') or '').lower()
    name = str(meta.get('filename') or '').lower()
    if name.endswith('.asc'):
        name = name[:-4]
    _, _, extension = name.rpartition('.')
    known = _EXTENSION_CATEGORIES.get(extension)
    if meta.get('call_recording'):
        return 'video' if known == 'video' else 'audio'
    if known:
        return known
    for prefix in ('image/', 'audio/', 'video/'):
        if filetype.startswith(prefix):
            return prefix[:-1]
    return 'other'


def _format_size(size):
    try:
        size = float(size)
    except (TypeError, ValueError):
        return None
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024.0 or unit == 'GB':
            return '%.0f %s' % (size, unit) if unit == 'B' else '%.1f %s' % (size, unit)
        size /= 1024.0


def _format_duration(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    if seconds >= 3600:
        return '%d:%02d:%02d' % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return '%d:%02d' % (seconds // 60, seconds % 60)


_MEDIA_LABELS = (
    ('image/', 'Image'),
    ('audio/', 'Audio'),
    ('video/', 'Video'),
    ('text/', 'Text'),
    ('application/pdf', 'PDF'),
    ('application/zip', 'Archive'),
)


def _media_label(filename, filetype):
    filetype = (filetype or '').lower()
    for prefix, label in _MEDIA_LABELS:
        if filetype.startswith(prefix):
            return label
    name = (filename or '').lower()
    if name.endswith('.asc'):
        name = name[:-4]
    _, _, extension = name.rpartition('.')
    if extension and extension != name and len(extension) <= 5:
        return extension.upper()
    return filetype or 'File'


# Names a recorder generated rather than anybody chose.
_RECORDING_TITLES = (
    ('sylk-call-recording', 'Call recording'),
    ('sylk-video-recording', 'Video call recording'),
    ('audio-recording', 'Call recording'),
    ('sylk-conf-recording', 'Conference recording'),
    ('sylk-audio-recording', 'Audio recording'),
    ('sylk-recording', 'Audio recording'),
)


def recording_title(filename):
    """A readable title for a machine-named recording, or None (a chosen name is kept)."""
    name = str(filename or '')
    if name.endswith('.asc'):
        name = name[:-4]
    stem = name.rsplit('/', 1)[-1].lower()
    for prefix, title in _RECORDING_TITLES:
        if stem.startswith(prefix):
            return title
    return None


def file_transfer_summary(body, duration=None):
    """A static description of a file-transfer envelope, or None if it is not one.

    `duration` supplies a length the envelope does not carry (the server
    relays a fixed field set and duration is not among them).
    """
    meta = file_transfer_envelope(body)
    if meta is None:
        return None
    name = str(meta.get('filename'))
    if name.endswith('.asc'):
        name = name[:-4]
    title = recording_title(name)
    lines = [('\U0001F3A4 ' + title) if title else ('\U0001F4CE ' + name)]
    details = []
    label = None if title else _media_label(name, meta.get('filetype'))
    if label:
        details.append(label)
    size = _format_size(meta.get('filesize'))
    if size:
        details.append(size)
    length = _format_duration(meta.get('duration')) or _format_duration(duration)
    if length:
        details.append(length)
    if details:
        lines.append(' · '.join(details))
    if meta.get('error'):
        lines.append('⚠ %s' % meta['error'])
    return '\n'.join(lines)


def transfer_error_note(reason):
    """The body to hand merge_transfer_error; None clears a stored error."""
    return json.dumps({'error': reason} if reason else {})


def merge_transfer_error(stored, incoming):
    """Fold a failure note into a stored file-transfer envelope.

    The stored envelope is the authority on every other field. An RCS XML body
    and anything that is not a transfer envelope are returned untouched, and
    so is a body that would not change, so it is not rewritten on every retry.
    """
    try:
        note = json.loads(incoming) if isinstance(incoming, str) else (incoming or {})
    except (TypeError, ValueError):
        return stored
    if not isinstance(note, dict):
        return stored
    text = _text(stored)
    if text is None or text.lstrip().startswith('<'):
        return stored
    try:
        meta = json.loads(text)
    except (TypeError, ValueError):
        return stored
    if not isinstance(meta, dict) or not meta.get('filename'):
        return stored
    reason = note.get('error') or None
    if reason:
        if meta.get('error') == reason:
            return stored
        meta['error'] = reason
    else:
        if 'error' not in meta:
            return stored
        meta.pop('error', None)
    return json.dumps(meta)


# Previews
#
QUOTE_DIGEST_CHARS = 240
CONVERSATION_PREVIEW_CHARS = 100        # mobile buildLastMessage cuts at the same length

# Text a client writes on the user's behalf (meeting and live-location notes,
# share / meet-up / request announcements); mobile buildLastMessage skips the same.
_PREVIEW_SYNTHETIC_RE = re.compile('^(?:'
                                   'Meeting (?:request|expired|cancelled|stopped|succeeded)\\b'
                                   '|\U0001F4CD '
                                   '|You met\\b'
                                   '|I want to meet (?:up with you|with you, too!?)'
                                   '|I am sharing the location with you'
                                   '|Could you share your current location'
                                   ')')
_PREVIEW_ARRIVAL_RE = re.compile('arrived at the meeting point\\s*$', re.I)


def quote_digest(body, is_html=False, limit=QUOTE_DIGEST_CHARS):
    """One short, flat line describing a message, for quoting it.

    A file transfer quotes as its summary and HTML as its text; newlines are
    collapsed and the result is cut at `limit` with an ellipsis.
    """
    body = _text(body)
    if body is None:
        return None
    summary = file_transfer_summary(body)
    if summary is not None:
        body = summary
    elif is_html:
        body = html2txt(body)
    flat = ' '.join(body.split())
    if not flat:
        return None
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + '…'
    return flat


def conversation_preview(body, content_type, msgid=None, reaction_ids=()):
    """The line a conversation's contact row quotes for this message, or None.

    None means "not this one, look at an older message": only what somebody
    typed qualifies, as on mobile. Files, locations, calls, keys, call-ended
    and key-received notes, synthetic location/meeting announcements and
    one-tap reactions (a pure-emoji body whose id is in `reaction_ids`, the
    reply ids of the conversation) are passed over. The body must be
    cleartext; an armoured one returns None.
    """
    content_type = str(content_type or '')
    if not (content_type == 'text' or content_type.startswith('text/')):
        return None
    if content_type in KEY_CONTENT_TYPES:
        return None
    body = _text(body)
    if body is None or is_pgp_armoured(body):
        return None
    text = html2txt(body) if content_type == 'text/html' else body
    stripped = (text or '').strip()
    if not stripped:
        return None
    if ' call ended ' in stripped or 'Public key received' in stripped:
        return None
    if _PREVIEW_SYNTHETIC_RE.match(stripped) or _PREVIEW_ARRIVAL_RE.search(stripped):
        return None
    if msgid and str(msgid) in reaction_ids and len(stripped) <= 24 and is_pure_emoji(stripped):
        return None
    return quote_digest(stripped, is_html=False, limit=CONVERSATION_PREVIEW_CHARS)


# Call detail records (application/blink-call-detail-record)
#
# The record rides in the row's metadata column (and the CPIM metadata on the
# wire) next to a plain summary body. Fields: version, sessionId, direction,
# outcome, duration, remoteParty, source, and optionally displayName, status,
# reason, startTime, stopTime, timezone, fromTag, toTag, proxyIP, answeredBy,
# sipTraceUrl, media[], local{deviceId, streams, ...}.
#
CALL_RECORD_VERSION = 1

# Outcomes of an incoming call the user did not take.
MISSED_CALL_OUTCOMES = ('missed', 'voicemail', 'rejected')

# Outcomes a call bubble draws in the attention colour; cancelled and
# answered elsewhere are ordinary events.
CALL_ATTENTION_OUTCOMES = ('missed', 'voicemail', 'rejected', 'failed')

SIP_STATUS_PHRASES = {
    '400': 'Bad Request',             '403': 'Forbidden',
    '404': 'Not Found',               '406': 'Not Acceptable',
    '407': 'Proxy Authentication Required',
    '408': 'Request Timeout',         '410': 'Gone',
    '415': 'Unsupported Media Type',
    '480': 'Temporarily Unavailable',
    '481': 'Call Does Not Exist',     '484': 'Address Incomplete',
    '486': 'Busy Here',               '487': 'Request Terminated',
    '488': 'Not Acceptable Here',
    '500': 'Server Internal Error',   '502': 'Bad Gateway',
    '503': 'Service Unavailable',     '504': 'Server Time-out',
    '600': 'Busy Everywhere',         '603': 'Decline',
    '604': 'Does Not Exist Anywhere',
    '606': 'Not Acceptable',
}

# Which fields a better informed view of the call may correct. `local` is not
# among them: nobody else can produce it, so it is never overwritten.
_AUTHORITATIVE_CALL_FIELDS = ('duration', 'stopTime', 'status', 'reason', 'proxyIP', 'toTag', 'outcome', 'answeredBy', 'sipTraceUrl')

# How much of the call each kind of record saw: a row rebuilt from old history
# knows least, the device that only heard it ring less than the one that
# answered, which knows less than the proxy that carried the whole call.
CALL_SOURCE_RANK = {'migrated': 0, 'local': 1, 'device': 2, 'server': 3}

_CALL_LABELS = {
    ('incoming', 'completed'):          'Incoming call',
    ('incoming', 'missed'):             'Missed call',
    ('incoming', 'rejected'):           'Rejected call',
    ('incoming', 'voicemail'):          'Voicemail',
    ('incoming', 'answered_elsewhere'): 'Answered on another device',
    ('outgoing', 'completed'):          'Outgoing call',
    ('outgoing', 'cancelled'):          'Cancelled call',
    ('outgoing', 'failed'):             'Call failed',
    ('outgoing', 'rejected'):           'Call rejected',
    ('outgoing', 'missed'):             'No answer',
}


def sip_status_phrase(status):
    """A SIP status code as something a person can read: 'Busy Here (486)'."""
    status = str(status or '').strip()
    if not status:
        return 'unknown'
    phrase = SIP_STATUS_PHRASES.get(status)
    return '%s (%s)' % (phrase, status) if phrase else status


def dominant_media(streams):
    """One label for what was negotiated (the media_type column): video beats audio beats the rest."""
    if isinstance(streams, (str, bytes)):
        text = streams.decode() if isinstance(streams, bytes) else streams
        names = [part.strip() for part in text.split(',')]
    else:
        names = [str(part).strip() for part in (streams or ())]
    names = [name for name in names if name]
    for preferred in ('video', 'audio'):
        if preferred in names:
            return preferred
    return names[0] if names else 'audio'


def _call_time(value):
    """A record timestamp as ISO-8601, always with an offset (naive values are UTC).

    The record crosses devices in different time zones, so an unqualified
    instant is read as local time somewhere and shows the wrong hour.
    """
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        if getattr(value, 'tzinfo', None) is not None:
            return value.isoformat()
        return value.isoformat() + '+00:00'
    text = str(value).strip()
    if not text:
        return None
    if text.endswith('Z') or '+' in text[10:] or text[10:].count('-') > 0:
        return text
    return text.replace(' ', 'T', 1) + '+00:00'


def build_call_record(session_id, direction, outcome, duration=0, status=None, reason=None, remote_party='', display_name='',
                      start_time=None, stop_time=None, media=None, from_tag='', to_tag='', proxy_ip=None, call_timezone=None,
                      source='local', local=None, answered_by=None, sip_trace_url=None):
    """A call detail record. Every producer builds one here so a call seen
    live and the same call replayed from the server can be merged. Empty
    optional fields are left out: missing means unknown."""
    record = {
        'version': CALL_RECORD_VERSION,
        'sessionId': str(session_id or ''),
        'direction': str(direction or ''),
        'outcome': str(outcome or ''),
        'duration': int(duration or 0),
        'remoteParty': str(remote_party or ''),
        'source': str(source or 'local'),
    }
    optional = {
        'displayName': display_name,
        'status': None if status is None else str(status),
        'reason': reason,
        'startTime': _call_time(start_time),
        'stopTime': _call_time(stop_time),
        'timezone': call_timezone,
        'fromTag': from_tag,
        'toTag': to_tag,
        'proxyIP': proxy_ip,
        'answeredBy': answered_by,
        'sipTraceUrl': sip_trace_url,
    }
    for key, value in optional.items():
        if value:
            record[key] = value
    if media:
        record['media'] = list(media)
    if local:
        record['local'] = local
    return record


def _call_rank(record):
    return CALL_SOURCE_RANK.get(str((record or {}).get('source') or 'local'), 1)


def merge_call_records(stored, incoming):
    """One record from two views of the same call.

    A view may correct the authoritative fields only from equal or higher
    rank: the device that answered can turn this device's missed row into an
    answered one, and the missed row can never turn it back. `local` is never
    erased, and the merged record keeps the higher of the two sources.
    """
    if not stored:
        return incoming
    if not incoming:
        return stored
    merged = dict(stored)
    stored_rank, incoming_rank = _call_rank(stored), _call_rank(incoming)
    protect = incoming_rank < stored_rank
    for key, value in incoming.items():
        if key == 'source':
            continue
        if key == 'local':
            if value:
                merged['local'] = value
            continue
        if value in (None, '', [], {}):
            continue
        if protect and key in _AUTHORITATIVE_CALL_FIELDS:
            continue
        merged[key] = value
    if incoming_rank >= stored_rank:
        merged['source'] = incoming.get('source') or merged.get('source')
    return merged


def this_device_id():
    """This device's id (the bare instance id), or None when it cannot be read.

    None makes every call read as an ordinary one rather than answered elsewhere.
    """
    try:
        from sipsimple.configuration.settings import SIPSimpleSettings
        text = str(SIPSimpleSettings().instance_id or '').strip()
    except Exception:
        return None
    if text.lower().startswith('urn:uuid:'):
        text = text[9:]
    return text or None


def call_answered_elsewhere(record, device_id=None):
    """Whether this call was answered on another of the user's devices."""
    if not record or not device_id:
        return False
    answered_by = str(record.get('answeredBy') or '').strip()
    return bool(answered_by) and answered_by != str(device_id).strip()


def call_record(body, metadata=None):
    """The record carried by a call row: from the metadata column, else a JSON body, else None."""
    for candidate in (metadata, body):
        if not candidate:
            continue
        record = candidate
        if isinstance(record, bytes):
            try:
                record = record.decode()
            except Exception:
                continue
        if isinstance(record, str):
            try:
                record = json.loads(record)
            except (TypeError, ValueError):
                continue
        if isinstance(record, dict) and record.get('sessionId'):
            return record
    return None


def format_call_duration(seconds):
    """Seconds as M:SS or H:MM:SS, None for a call that never connected."""
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return ('%d:%02d:%02d' % (hours, minutes, secs)) if hours else ('%d:%02d' % (minutes, secs))


def call_outcome(record):
    """`outcome`, or the derivation every client did before the field existed."""
    outcome = str(record.get('outcome') or '').strip()
    if outcome:
        return outcome
    try:
        duration = int(record.get('duration') or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        return 'completed'
    if str(record.get('direction') or '') == 'outgoing':
        return 'cancelled' if str(record.get('status') or '').strip() == '487' else 'failed'
    return 'missed'


def call_was_missed(record):
    """Whether a call record is an incoming call the user did not take."""
    if not record:
        return False
    return str(record.get('direction') or '') == 'incoming' and call_outcome(record) in MISSED_CALL_OUTCOMES


def call_needs_attention(record, device_id=None):
    """Whether this call is one the reader's eye should stop on."""
    if not record:
        return False
    outcome = call_outcome(record)
    if outcome == 'completed' and call_answered_elsewhere(record, device_id):
        return False
    return outcome in CALL_ATTENTION_OUTCOMES


def call_lines(record, device_id=None):
    """(title, duration, reason) describing a call, or None for a record too broken to describe."""
    if not record:
        return None
    direction = str(record.get('direction') or 'incoming')
    outcome = call_outcome(record)
    if outcome == 'completed' and call_answered_elsewhere(record, device_id):
        outcome = 'answered_elsewhere'
    label = _CALL_LABELS.get((direction, outcome))
    if label is None:
        return None
    # what this device negotiated wins over what the proxy saw
    local = record.get('local') if isinstance(record.get('local'), dict) else {}
    streams = local.get('streams') or record.get('media') or []
    if 'video' in streams:
        label = label.replace('call', 'video call')
    duration = format_call_duration(record.get('duration')) or ''
    phrase = ''
    if outcome in ('failed', 'rejected'):
        status = str(record.get('status') or '').strip()
        reason = str(record.get('reason') or '').strip()
        if status in SIP_STATUS_PHRASES:
            phrase = sip_status_phrase(status)
        else:
            phrase = reason or (sip_status_phrase(status) if status else '')
        if phrase == 'unknown':
            phrase = ''
    return label, duration, phrase


def call_summary(record, device_id=None):
    """The one plain-text line a call says: the stored body, a notification, the contact row.

    Computed when drawn, so a row written by an older build says the right
    thing without a migration.
    """
    lines = call_lines(record, device_id)
    if lines is None:
        return None
    label, duration, phrase = lines
    parts = [label]
    if duration:
        parts.append('(%s)' % duration)
    if phrase:
        parts.append('— %s' % phrase)
    return ' '.join(parts)


# Metadata sidecars (application/sylk-message-metadata)
#
REPLY_ACTION = 'reply'
LABEL_ACTION = 'label'
PEAKS_ACTION = 'peaks'
CALL_RECORDING_ACTION = 'call_recording'


def _envelope(body, action):
    body = _text(body)
    if body is None or action not in body:
        return None
    try:
        envelope = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or envelope.get('action') != action:
        return None
    return envelope


def reply_metadata(body):
    """The reply link carried by a metadata envelope, or None.

    Mobile sends the link as a separate message next to the reply:
    {"messageId": <reply id>, "metadataId", "action": "reply", "value": <original id>, "timestamp", "uri"}.
    Either half may arrive first. Returns {'reply_id', 'original_id', 'metadata_id'}.
    """
    envelope = _envelope(body, REPLY_ACTION)
    if envelope is None:
        return None
    reply_id = envelope.get('messageId')
    original_id = envelope.get('value')
    if not reply_id or not original_id:
        return None
    return {'reply_id': str(reply_id), 'original_id': str(original_id), 'metadata_id': str(envelope.get('metadataId') or '')}


def reply_envelope(reply_id, original_id, metadata_id, peer_uri, timestamp):
    """The body of the companion message that records a reply (mobile's fields)."""
    return json.dumps({'messageId': str(reply_id),
                       'metadataId': str(metadata_id),
                       'action': REPLY_ACTION,
                       'value': str(original_id),
                       'timestamp': str(timestamp),
                       'uri': str(peer_uri)})


def label_metadata(body):
    """A caption set on a picture or movie, or None.

    {"messageId": <TRANSFER id>, "metadataId", "action": "label", "value": <caption>, "timestamp", "uri"}.
    The newest wins and an empty value clears it. Returns
    {'transfer_id', 'label', 'timestamp', 'metadata_id'}; label is '' when cleared.
    """
    envelope = _envelope(body, LABEL_ACTION)
    if envelope is None:
        return None
    transfer_id = envelope.get('messageId')
    if not transfer_id:
        return None
    value = envelope.get('value')
    return {'transfer_id': str(transfer_id),
            'label': value.strip() if isinstance(value, str) else '',
            'timestamp': str(envelope.get('timestamp') or ''),
            'metadata_id': str(envelope.get('metadataId') or '')}


def label_envelope(transfer_id, metadata_id, label, peer_uri, timestamp):
    """The body of the companion message that sets a caption.

    Compact separators, as JSON.stringify writes them: the tombstone of a
    removed transfer finds its sidecars with LIKE '%"messageId":"<id>"%'.
    """
    return json.dumps({'messageId': str(transfer_id),
                       'metadataId': str(metadata_id),
                       'action': LABEL_ACTION,
                       'value': str(label or ''),
                       'timestamp': str(timestamp),
                       'uri': str(peer_uri)},
                      separators=(',', ':'))


def peaks_metadata(body):
    """A recording's waveform and spectrogram, or None.

    {"messageId": <TRANSFER id>, "metadataId", "action": "peaks",
     "value": {"l": [...], "r": [...], "spectrum": {...}}, "timestamp"}.
    Returns {'transfer_id', 'peaks': {'l', 'r'}, 'spectrum'}; None for an all-empty payload.
    """
    envelope = _envelope(body, PEAKS_ACTION)
    if envelope is None:
        return None
    transfer_id = envelope.get('messageId')
    value = envelope.get('value')
    if not transfer_id or not isinstance(value, dict):
        return None
    peaks = {}
    for channel in ('l', 'r'):
        samples = value.get(channel)
        peaks[channel] = list(samples) if isinstance(samples, (list, tuple)) else []
    if not peaks['l'] and not peaks['r']:
        return None
    spectrum = value.get('spectrum')
    if not isinstance(spectrum, dict) or not spectrum.get('data'):
        spectrum = None
    return {'transfer_id': str(transfer_id), 'peaks': peaks, 'spectrum': spectrum}


def peaks_envelope(transfer_id, metadata_id, peaks, spectrum, peer_uri, timestamp):
    """The body of the companion message that carries a waveform (mobile's fields)."""
    value = {'l': list((peaks or {}).get('l') or []),
             'r': list((peaks or {}).get('r') or [])}
    if spectrum:
        value['spectrum'] = spectrum
    return json.dumps({'messageId': str(transfer_id),
                       'metadataId': str(metadata_id),
                       'action': PEAKS_ACTION,
                       'value': value,
                       'timestamp': str(timestamp),
                       'uri': str(peer_uri)})


def call_recording_envelope(transfer_id, party_uri, display_name, duration, timestamp, encrypt=None):
    """The body of the companion message that places a call recording.

    A recording is uploaded from the account to itself, so which conversation
    it belongs to travels separately, keyed on the transfer id. Who the call
    was with is armoured to the account's own key by `encrypt` (plaintext ->
    armour); without one this returns None rather than a party in clear.
    """
    value = {'uri': str(party_uri)}
    if display_name:
        value['display_name'] = str(display_name)
    if duration:
        try:
            value['duration'] = round(float(duration), 2)
        except (TypeError, ValueError):
            pass
    if encrypt is None:
        return None
    armoured = encrypt(json.dumps(value))
    if not armoured:
        return None
    return json.dumps({'fileTransferId': str(transfer_id),
                       'action': CALL_RECORDING_ACTION,
                       'value': str(armoured),
                       'timestamp': str(timestamp)})


def call_recording_metadata(body, decrypt=None):
    """Where a call recording belongs, or None.

    {"fileTransferId", "action": "call_recording", "value": <armoured {"uri", "display_name", "duration"}>, "timestamp"}.
    `decrypt` takes the armour and returns the plaintext, or None without a
    key. An unarmoured string value (a party in clear) is refused. Returns
    {'transfer_id', 'uri', 'display_name', 'duration'}.
    """
    envelope = _envelope(body, CALL_RECORDING_ACTION)
    if envelope is None:
        return None
    transfer_id = str(envelope.get('fileTransferId') or '').strip()
    value = envelope.get('value')
    if isinstance(value, str):
        if not value.lstrip().startswith('-----BEGIN PGP MESSAGE-----') or decrypt is None:
            return None
        try:
            value = json.loads(decrypt(value) or '')
        except (ValueError, TypeError):
            return None
    if not transfer_id or not isinstance(value, dict):
        return None
    uri = str(value.get('uri') or '').strip()
    if not uri:
        return None
    duration = value.get('duration')
    try:
        duration = round(float(duration), 2) if duration else None
    except (TypeError, ValueError):
        duration = None
    return {'transfer_id': transfer_id, 'uri': uri, 'display_name': str(value.get('display_name') or '') or None, 'duration': duration}


# Category and link columns
#
def classify_category(content_type, body=None, related_action=None, metadata=None):
    """Which category filter a stored row belongs to, or None for a non-bubble.

    Stamped at insert time so "the last fifty images" is an indexed query.
    Nothing here decrypts: a file transfer is classified from its cleartext
    envelope (armoured or unreadable -> None, stamped later when decrypted)
    and a location from its cleartext envelope by blink.location.
    """
    content_type = str(content_type or '')
    if not content_type:
        return None
    if content_type in KEY_CONTENT_TYPES:
        return None
    if content_type in (CALL_CONTENT_TYPE, LEGACY_CALL_CONTENT_TYPE):
        return 'call'
    if content_type in TEXT_CONTENT_TYPES or content_type.startswith('text/'):
        return 'text'
    if content_type in FILE_TRANSFER_CONTENT_TYPES:
        return file_transfer_category(body)
    if content_type == LOCATION_CONTENT_TYPE:
        # Only a coordinate origin or a one-shot is a browsable location;
        # trail ticks, start/stop and meeting signals are not.
        try:
            from blink.location import envelope_summary
        except ImportError:
            return None
        try:
            summary = envelope_summary(body, metadata, content_type)
        except Exception:
            return None
        return (summary or {}).get('category')
    return None


# Same pattern the macOS bubble links with.
_url_re = re.compile(r'((?:https?://|sip:|sips:)[^\s<>()\[\]"\']+)')


def has_link(content_type, text):
    """1 if a text/plain or text/html body contains a link, else 0 (the has_link column).

    None for an armoured body, as on mobile: unknown until it is decrypted.
    """
    if content_type not in ('text/plain', 'text/html'):
        return 0
    text = _text(text)
    if not text:
        return 0
    if is_pgp_armoured(text):
        return None
    return 1 if _url_re.search(text) else 0


# Keys
#
def public_key_id(armored_key):
    """The OpenPGP long key id of an armoured key (last 16 hex of the fingerprint), or None.

    The key's own id rather than a hash of the armour: it is what an
    encrypted message names and what survives the key being re-armoured.
    """
    if armored_key is None:
        return None
    if isinstance(armored_key, bytes):
        armored_key = armored_key.decode('utf-8', 'replace')
    try:
        import pgpy
        key, _ = pgpy.PGPKey.from_blob(armored_key)
        return str(key.fingerprint.keyid)
    except Exception:
        return None
