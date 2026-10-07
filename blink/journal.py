"""SylkServer message journal: dispatch rules and the on-disk page cache.

The journal is downloaded page by page into journal/<account>/ and applied
from there (blink.messages.MessageManager). What happens to each entry is
decided here, by content type, the same table Blink for macOS uses
(inventory §6.3). No Qt and no sipsimple, so the rules can be tested alone.
"""

import ast
import json
import os
import threading
import time

from collections import Counter, OrderedDict, defaultdict

from blink.message_envelopes import (ADDRESSBOOK_UPDATE_CONTENT_TYPE, CALL_CONTENT_TYPE, CONTACT_UPDATE_CONTENT_TYPE, CONVERSATION_READ_CONTENT_TYPE,
                                     CONVERSATION_REMOVE_CONTENT_TYPE, DATA_EXPORT_CONTENT_TYPE, FILE_TRANSFER_CONTENT_TYPE, LOCATION_CONTENT_TYPE,
                                     MESSAGE_REMOVE_CONTENT_TYPE, METADATA_CONTENT_TYPE, PGP_PRIVATE_KEY_CONTENT_TYPE, PGP_PUBLIC_KEY_CONTENT_TYPE,
                                     RCS_FILE_TRANSFER_CONTENT_TYPE)


__all__ = ['journal_action', 'parse_payload', 'JournalCache', 'JournalStats', 'SeenMessageIds', 'OwnMarkers', 'IGNORED_CONTENT_TYPES', 'KNOWN_INERT_CONTENT_TYPES', 'MAX_PAGE_ATTEMPTS']


IMDN_CONTENT_TYPE = 'message/imdn'
ISCOMPOSING_CONTENT_TYPE = 'application/im-iscomposing+xml'

# Entries that are not history: they are acted on live or not at all.
IGNORED_CONTENT_TYPES = frozenset((ADDRESSBOOK_UPDATE_CONTENT_TYPE, DATA_EXPORT_CONTENT_TYPE, CONTACT_UPDATE_CONTENT_TYPE,
                                   ISCOMPOSING_CONTENT_TYPE, PGP_PRIVATE_KEY_CONTENT_TYPE))

# Stored as they are for now and handled by their own patches; anything else
# stored inert is a type this version does not know (the UNHANDLED line).
KNOWN_INERT_CONTENT_TYPES = frozenset((LOCATION_CONTENT_TYPE, METADATA_CONTENT_TYPE, RCS_FILE_TRANSFER_CONTENT_TYPE))

# A page that fails this many runs in a row is put aside so the pages after it are applied.
MAX_PAGE_ATTEMPTS = 3


def journal_action(content_type):
    """What to do with a journal entry of this content type.

    'receipt'              message/imdn: delivery state of a message (skipped on a first sync)
    'conversation_remove'  hide a conversation up to the removal time
    'message_remove'       hide one message (kept until it arrives if it is not stored yet)
    'conversation_read'    mark a conversation read
    'public_key'           save a peer's PGP public key
    'file_transfer'        store a Sylk file transfer
    'call_record'          a call another of the user's devices took part in, merged by Call-ID
    'text'                 store a text message
    'ignored'              not history (address book, data export, contact update, typing, private key)
    'inert'                anything else: stored as it is, never unread (locations, metadata
                           companions and types this version does not know)
    """
    content_type = str(content_type or '').strip().lower()
    if content_type == IMDN_CONTENT_TYPE:
        return 'receipt'
    if content_type == CONVERSATION_REMOVE_CONTENT_TYPE:
        return 'conversation_remove'
    if content_type == MESSAGE_REMOVE_CONTENT_TYPE:
        return 'message_remove'
    if content_type == CONVERSATION_READ_CONTENT_TYPE:
        return 'conversation_read'
    if content_type == PGP_PUBLIC_KEY_CONTENT_TYPE:
        return 'public_key'
    if content_type in IGNORED_CONTENT_TYPES or content_type.startswith('application/sylk-api'):
        return 'ignored'
    if content_type == FILE_TRANSFER_CONTENT_TYPE:
        return 'file_transfer'
    if content_type == CALL_CONTENT_TYPE:
        return 'call_record'
    if content_type.startswith('text/'):
        return 'text'
    return 'inert'


def parse_payload(content):
    """A JSON object payload (receipt, removal), or None.

    Older senders wrote Python reprs; those are read with literal_eval,
    never eval: the content is data from the server.
    """
    if isinstance(content, dict):
        return content
    if isinstance(content, bytes):
        content = content.decode('utf-8', 'replace')
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        value = json.loads(content)
    except ValueError:
        try:
            value = ast.literal_eval(content)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None
    return value if isinstance(value, dict) else None


class JournalCache(object):
    """journal/<account>/: cached pages, their failed attempts and the quarantine.

    A page is a JSON file {"cursor", "messages"} named so names sort
    chronologically. Pages are applied oldest first and deleted once applied.
    A page that keeps failing is moved to quarantine/ after MAX_PAGE_ATTEMPTS
    runs, so one bad page cannot hold back everything after it for ever.
    """

    attempts_file = 'attempts.json'
    quarantine_directory = 'quarantine'

    def __init__(self, directory, max_attempts=MAX_PAGE_ATTEMPTS):
        self.directory = directory
        self.max_attempts = max_attempts

    def pages(self):
        try:
            return sorted(name for name in os.listdir(self.directory) if name.endswith('.json') and name != self.attempts_file)
        except FileNotFoundError:
            return []

    def path(self, name):
        return os.path.join(self.directory, name)

    def load(self, name):
        with open(self.path(name), encoding='utf-8') as page_file:
            page = json.load(page_file)
        if not isinstance(page, dict) or not isinstance(page.get('messages', []), list):
            raise ValueError('not a journal page')
        return page

    def _attempts(self):
        try:
            with open(self.path(self.attempts_file), encoding='utf-8') as attempts_file:
                attempts = json.load(attempts_file)
            return attempts if isinstance(attempts, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_attempts(self, attempts):
        path = self.path(self.attempts_file)
        if not attempts:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return
        with open(path + '.part', 'w', encoding='utf-8') as attempts_file:
            json.dump(attempts, attempts_file)
        os.replace(path + '.part', path)

    def applied(self, name):
        """The page was applied: delete it and forget its failures."""
        try:
            os.unlink(self.path(name))
        except FileNotFoundError:
            pass
        attempts = self._attempts()
        if attempts.pop(name, None) is not None:
            self._save_attempts(attempts)

    def failed(self, name):
        """Count a failed attempt; returns (attempts, quarantined)."""
        attempts = self._attempts()
        count = attempts.get(name, 0) + 1
        if count < self.max_attempts:
            attempts[name] = count
            self._save_attempts(attempts)
            return count, False
        quarantine = os.path.join(self.directory, self.quarantine_directory)
        os.makedirs(quarantine, exist_ok=True)
        os.replace(self.path(name), os.path.join(quarantine, name))
        attempts.pop(name, None)
        self._save_attempts(attempts)
        return count, True


class SeenMessageIds(object):
    """The ids of the last `size` messages handled, live or from the journal.

    The same message can come both ways (live, then replayed by the journal,
    or the other way round when delivery is late); the second one is skipped
    so it is neither stored again nor shown twice in an open conversation.
    Used from the GUI and the sync threads, hence the lock.
    """

    def __init__(self, size=10000):
        self.size = size
        self._ids = OrderedDict()
        self._lock = threading.Lock()

    def __contains__(self, message_id):
        with self._lock:
            return message_id in self._ids

    def __len__(self):
        return len(self._ids)

    def seen(self, message_id):
        """True if the id was handled before; else remember it and return False.
        A message without an id is never a duplicate."""
        if not message_id:
            return False
        message_id = str(message_id)
        with self._lock:
            if message_id in self._ids:
                return True
            self._ids[message_id] = None
            while len(self._ids) > self.size:
                self._ids.popitem(last=False)
            return False

    def forget(self, message_id):
        """Drop an id whose handling failed, so a later copy is handled again."""
        with self._lock:
            self._ids.pop(str(message_id), None)


class OwnMarkers(object):
    """Markers this device has just sent (conversation read), by conversation key.

    The server fans a marker out to every device of the account, this one
    included. A marker carrying the sender's device id settles it; for one
    without (a server that rebuilds the body) a send in the last `ttl`
    seconds accounts for one echo.
    """

    def __init__(self, ttl=30, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._sent = {}
        self._lock = threading.Lock()

    def note(self, key):
        if key:
            with self._lock:
                self._sent.setdefault(key, []).append(self.clock())

    def is_echo(self, key, device_id, this_device_id):
        """Whether a marker for `key` from `device_id` is the copy of one this device sent."""
        with self._lock:
            horizon = self.clock() - self.ttl
            pending = [sent for sent in self._sent.get(key, []) if sent >= horizon] if key else []
            echo = (device_id == this_device_id) if device_id else bool(pending)
            if echo and pending:
                pending.pop(0)
            if key:
                if pending:
                    self._sent[key] = pending
                else:
                    self._sent.pop(key, None)
            return echo


class JournalStats(object):
    """What one journal run did, comparable between runs (plan §1.4).

    Filled by the download (pages) and the apply (entries); written to the
    activity log as a summary and to logs/import-<account>-<time>.json, so two
    from-scratch runs of the same account can be diffed.
    """

    def __init__(self, account_id, first_sync=False, cursor=None, reason=None):
        self.account_id = str(account_id)
        self.first_sync = bool(first_sync)
        self.cursor = cursor or None
        self.reason = reason
        self.started = time.time()
        self.pages = []
        self.download_seconds = 0.0
        self.apply_seconds = 0.0
        self.by_type = defaultdict(Counter)     # content type -> outcome -> count
        self.by_contact = {}                    # contact -> {types, incoming, first, last}
        self.quarantined = []

    def page_downloaded(self, name, entries, transferred, seconds, cursor):
        self.pages.append({'file': name, 'entries': entries, 'bytes': transferred, 'seconds': round(seconds, 3), 'cursor': cursor})
        self.download_seconds += seconds

    def entry(self, content_type, outcome, contact=None, direction=None, timestamp=None):
        content_type = str(content_type or '').lower() or '(none)'
        self.by_type[content_type]['received'] += 1
        self.by_type[content_type][outcome] += 1
        if not contact:
            return
        record = self.by_contact.setdefault(str(contact), {'types': Counter(), 'incoming': 0, 'first': None, 'last': None})
        record['types'][content_type] += 1
        if direction == 'incoming':
            record['incoming'] += 1
        if timestamp:
            timestamp = str(timestamp)
            if record['first'] is None or timestamp < record['first']:
                record['first'] = timestamp
            if record['last'] is None or timestamp > record['last']:
                record['last'] = timestamp

    @property
    def entries(self):
        return sum(counts['received'] for counts in self.by_type.values())

    def outcomes(self):
        totals = Counter()
        for counts in self.by_type.values():
            totals.update({outcome: count for outcome, count in counts.items() if outcome != 'received'})
        return totals

    def unhandled(self):
        return {content_type: counts['received'] for content_type, counts in self.by_type.items()
                if journal_action(content_type) == 'inert' and content_type not in KNOWN_INERT_CONTENT_TYPES}

    def summary_lines(self, top=50):
        """The run summary for the activity log."""
        lines = [f'Journal run of {self.account_id}: {self.entries} entries, {len(self.pages)} pages downloaded in {self.download_seconds:.1f}s,'
                 f' applied in {self.apply_seconds:.1f}s' + (' (first sync)' if self.first_sync else '')]
        for content_type, counts in sorted(self.by_type.items(), key=lambda item: -item[1]['received']):
            details = ', '.join(f'{count} {outcome}' for outcome, count in sorted(counts.items()) if outcome != 'received')
            lines.append(f'  {content_type}: {counts["received"]} received ({details})')
        contacts = sorted(self.by_contact.items(), key=lambda item: -sum(item[1]['types'].values()))
        if contacts:
            lines.append(f'  {len(contacts)} conversations' + (f', the {top} largest:' if len(contacts) > top else ':'))
        for contact, record in contacts[:top]:
            types = ', '.join(f'{count} {content_type}' for content_type, count in record['types'].most_common())
            lines.append(f'    {contact}: {sum(record["types"].values())} entries, {record["incoming"]} incoming ({types}), {record["first"]} .. {record["last"]}')
        unhandled = self.unhandled()
        if unhandled:
            lines.append('  UNHANDLED ' + ', '.join(f'{content_type} x{count}' for content_type, count in sorted(unhandled.items())))
        for name in self.quarantined:
            lines.append(f'  QUARANTINED {name}')
        return lines

    def as_dict(self):
        return {'account': self.account_id,
                'started': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.started)),
                'reason': self.reason,
                'first_sync': self.first_sync,
                'cursor': self.cursor,
                'entries': self.entries,
                'download_seconds': round(self.download_seconds, 3),
                'apply_seconds': round(self.apply_seconds, 3),
                'pages': self.pages,
                'outcomes': dict(self.outcomes()),
                'content_types': {content_type: dict(counts) for content_type, counts in sorted(self.by_type.items())},
                'conversations': {contact: {'entries': sum(record['types'].values()), 'incoming': record['incoming'], 'types': dict(record['types']),
                                            'first': record['first'], 'last': record['last']}
                                  for contact, record in sorted(self.by_contact.items())},
                'unhandled': self.unhandled(),
                'quarantined': self.quarantined}

    def write(self, directory):
        """Write the run as logs/import-<account>-<time>.json; returns the path."""
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(self.started))
        path = os.path.join(directory, f'import-{self.account_id}-{stamp}.json')
        with open(path, 'w', encoding='utf-8') as stats_file:
            json.dump(self.as_dict(), stats_file, indent=1, sort_keys=True)
        return path
