"""Managing the message history in bulk: what the Data window drills into, and
removing what was chosen from the journal on the server.

Every stored row of an account is given a type: its category when it has one
(text, image, call, ...), 'location' for location sharing, otherwise its
content type; and for location sharing and the uncategorized types an action
(location_start, location_update, location_stop; reply, label, peaks for
metadata; 'encrypted' for a PGP body nobody decrypted, whose action cannot be
known). The window narrows by type, action, contact and year/month/day, the
way the data import does, and removes the rows selected:
here, and from the account's journal on the server through the bulk removal
endpoint, so a new device no longer downloads them.

No Qt here, so the classification and the requests can be tested alone.
"""

import json
import os
import re
import uuid

from collections import Counter, namedtuple
from datetime import datetime, timedelta, timezone

import requests

from blink.file_transfer import FAILURE_GONE, safe_component, transfer_filename
from blink.message_envelopes import LOCATION_CONTENT_TYPE, METADATA_CONTENT_TYPE, file_transfer_envelope


__all__ = ['with_related', 'period_bounds', 'bulk_delete_payload', 'bulk_delete_criteria', 'criteria_matches_action', 'criteria_label', 'BULK_DELETE_CATEGORIES',
           'Row', 'ENCRYPTED', 'classify', 'type_label', 'action_label', 'row_label', 'build_rows', 'select', 'count_by', 'aggregate_days',
           'FileRow', 'FILE_KINDS', 'ON_SERVER', 'FAILED', 'NO_KEY', 'DOWNLOADABLE', 'status_label', 'scan_transfer_storage', 'build_file_rows',
           'REMOVE_BATCH', 'PROGRESS_EVERY', 'JournalRemoveError', 'JournalRemoveUnsupported', 'JournalRemoveRefused', 'journal_remove_url', 'remove_from_journal',
           'JournalEntry', 'JournalScanError', 'journal_page_url', 'scan_journal', 'journal_summary', 'purge_journal', 'parse_age', 'JOURNAL_SINCE_YEARS',
           'CompactionRule', 'COMPACTION_RULES', 'compaction_rule', 'compaction_ids', 'compaction_entries', 'compaction_content_types', 'contact_breakdown']


CATEGORY_LABELS = {'text': 'Text', 'image': 'Images', 'audio': 'Audio', 'video': 'Videos', 'other': 'Other files', 'call': 'Calls',
                   'location': 'Location sharing'}

TYPE_LABELS = {METADATA_CONTENT_TYPE: 'Metadata', 'application/sylk-request': 'Request'}

ENCRYPTED = 'encrypted'

# The action of a row, in the first characters of its body: "action" for metadata and location
# sharing, "request_type" for application/sylk-request. A key's spelling may be followed by a space
# (Python's json.dumps) or not (JavaScript's JSON.stringify).
_ACTION = re.compile(r'"(?:action|request_type)"\s*:\s*"([^"\\]{1,64})"')

PREVIEW_LENGTH = 160


Row = namedtuple('Row', 'id message_id contact day timestamp direction type action preview related')


def classify(category, content_type, related_action, head):
    """(type, action) of a row.

    The type is 'location' for every location sharing row (the start of a share is stored with
    category location, its ticks without one), else the category, else the content type. The
    action tells apart the rows of an uncategorized type and of location sharing: the one stored
    with the row (in clear even when the body is encrypted), else the one in the body, else
    'encrypted' for a PGP body nobody decrypted -- its action cannot be known. Categorized rows
    (text, images, calls, ...) have none.
    """
    content_type = str(content_type or '').strip().lower() or '(none)'
    if content_type == LOCATION_CONTENT_TYPE:
        kind = 'location'
    elif category:
        return str(category), ''
    else:
        kind = content_type
    if related_action:
        return kind, str(related_action)
    text = str(head or '').lstrip()
    if text.startswith('-----BEGIN PGP'):
        return kind, ENCRYPTED
    match = _ACTION.search(text)
    return kind, match.group(1) if match else ''


def type_label(kind):
    if kind is None:
        return 'All types'
    if kind in CATEGORY_LABELS:
        return CATEGORY_LABELS[kind]
    return TYPE_LABELS.get(kind) or kind.replace('application/', '').replace('sylk-', '')


def action_label(action):
    if action is None:
        return 'All actions'
    if action == ENCRYPTED:
        return 'encrypted (action unknown)'
    return action or '(none)'


def row_label(row):
    return f'{type_label(row.type)} · {action_label(row.action)}' if row.action else type_label(row.type)


def _preview(head):
    text = str(head or '').lstrip()
    if text.startswith('-----BEGIN PGP'):
        return '(PGP encrypted)'
    text = ' '.join(text.split())
    return text[:PREVIEW_LENGTH] + ('…' if len(text) > PREVIEW_LENGTH else '')


def build_rows(db_rows):
    """Rows from MessageHistory.data_inventory tuples."""
    rows = []
    for row_id, message_id, remote_uri, day, timestamp, direction, category, content_type, related_action, head, related_msg_id in db_rows:
        kind, action = classify(category, content_type, related_action, head)
        rows.append(Row(int(row_id), str(message_id) if message_id else None, str(remote_uri or ''), str(day or ''), str(timestamp or ''),
                        str(direction or ''), kind, action, _preview(head), str(related_msg_id) if related_msg_id else None))
    return rows


def with_related(rows, selected):
    """The selected rows followed by the rows filed against them (related_msg_id: location ticks of a
    share, reply/label/peaks sidecars of a message), all the way down."""
    children = {}
    for row in rows:
        if row.related and row.related != row.message_id:
            children.setdefault(row.related, []).append(row)
    chosen = {row.id for row in selected}
    result = list(selected)
    frontier = [row.message_id for row in selected if row.message_id]
    seen = set(frontier)
    while frontier:
        found = []
        for message_id in frontier:
            for row in children.get(message_id, ()):
                if row.id in chosen:
                    continue
                chosen.add(row.id)
                result.append(row)
                if row.message_id and row.message_id not in seen:
                    seen.add(row.message_id)
                    found.append(row.message_id)
        frontier = found
    return result


def select(rows, kind=None, action=None, contact=None, period=None):
    """The rows of a type, an action, a contact and a period ('YYYY', 'YYYY-MM' or 'YYYY-MM-DD'); None is any."""
    return [row for row in rows
            if (kind is None or row.type == kind)
            and (action is None or row.action == action)
            and (contact is None or row.contact == contact)
            and (not period or row.day.startswith(period))]


def count_by(rows, attribute):
    """[(value, count)] most first, then by value."""
    counts = Counter(getattr(row, attribute) for row in rows)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def aggregate_days(rows, length, prefix=None):
    """[(day[:length], count)] newest first, optionally within one prefix (a year, a month)."""
    counts = Counter(row.day[:length] for row in rows if row.day and (not prefix or row.day.startswith(prefix)))
    return sorted(counts.items(), reverse=True)


# Removal from the journal on the server
#
# POST <journal url>/remove {"ids": [...], "progress": N} with the account's API token, at most
# REMOVE_BATCH ids per request. The server removes only this account's rows, writes no tombstone and
# tells no other device. With "progress" it streams JSON lines while it works, {"done", "removed",
# "total"} every N ids, and ends with {"ok", "requested", "removed": [ids]}; a server that does not
# know "progress" sends only that last line. A server without the endpoint answers 404 or 405.

REMOVE_BATCH = 1000
PROGRESS_EVERY = 100


class JournalRemoveError(Exception):
    pass


class JournalRemoveUnsupported(JournalRemoveError):
    pass


class JournalRemoveRefused(JournalRemoveError):
    pass


def journal_remove_url(history_url):
    return str(history_url).rstrip('/') + '/remove'


def remove_from_journal(history_url, token, message_ids, verify=True, timeout=(15, 120), progress=None, every=PROGRESS_EVERY, request_id=None):
    """Remove up to REMOVE_BATCH message ids from the journal; returns the set of ids removed.
    progress(done, removed, total) is called as the server reports, from the calling thread;
    the read timeout is the longest silence allowed between two reports."""
    message_ids = [str(message_id) for message_id in message_ids if message_id]
    if not message_ids:
        return set()
    if len(message_ids) > REMOVE_BATCH:
        raise ValueError(f'at most {REMOVE_BATCH} ids per request')
    url = journal_remove_url(history_url)
    body = {'ids': message_ids}
    if request_id:
        body['request_id'] = str(request_id)       # names the request in the server log
    if progress is not None:
        body['progress'] = int(every)
    try:
        response = requests.post(url, json=body, headers={'Authorization': f'Apikey {token}'}, timeout=timeout, verify=verify, stream=True)
    except requests.RequestException as e:
        raise JournalRemoveError(f'cannot reach {url}: {e}')
    with response:
        if response.status_code in (404, 405):
            raise JournalRemoveUnsupported(f'the server has no bulk removal (HTTP {response.status_code} from {url})')
        if response.status_code == 401:
            raise JournalRemoveRefused('the server refused the API token (HTTP 401)')
        if response.status_code != 200:
            detail = ''
            try:
                detail = json.loads(response.content).get('error') or ''
            except (ValueError, AttributeError):
                pass
            raise JournalRemoveError(f'HTTP {response.status_code} from {url}' + (f': {detail}' if detail else ''))
        final = None
        try:
            for line in response.iter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    raise JournalRemoveError(f'{url} did not answer JSON')
                if not isinstance(data, dict):
                    continue
                if isinstance(data.get('removed'), list):
                    final = data
                elif 'done' in data and progress is not None:
                    progress(int(data.get('done') or 0), int(data.get('removed') or 0), int(data.get('total') or len(message_ids)))
        except requests.RequestException as e:
            raise JournalRemoveError(f'the answer of {url} was cut off: {e}')
    if final is None:
        raise JournalRemoveError(f'{url} answered without a list of removed ids')
    return {str(message_id) for message_id in final['removed']}


# Compacting the journal on the server
#
# The journal keeps every entry of the account for years: what a new device downloads to start, and
# what a device that was offline downloads to catch up. Some entries stop being worth that download
# once they are old, and some are kept nowhere once applied (delivery and read receipts are applied
# to the message they are about), so they cannot be chosen in the Local messages tab. Compacting
# walks the journal page by page as a sync does (GET <journal url>?since=... for the first page,
# <journal url>/<last id> for each next one), picks the entries of the chosen rules older than
# their age, and removes them with remove_from_journal. Nothing is changed on any device: the
# devices that have the entries keep what they made of them. Used by the Compact journal tab of
# the Data window and by scripts/compact_journal.py.

JOURNAL_SINCE_YEARS = 5     # how far back the walk starts, as a first sync asks (MessageManager.journal_since_years)

JournalEntry = namedtuple('JournalEntry', 'message_id timestamp content_type action contact')

CompactionRule = namedtuple('CompactionRule', 'name title content_types actions age description')

IMDN_JOURNAL_CONTENT_TYPE = 'message/imdn'

COMPACTION_RULES = (
    CompactionRule('receipts', 'Delivery and read receipts', frozenset((IMDN_JOURNAL_CONTENT_TYPE,)), None, timedelta(days=30),
                   'Every delivery and read receipt (message/imdn) is an entry of its own. A device that has '
                   'synchronized applied it to the state of its message and keeps nothing else of it. '
                   'Removed, a device that downloads the journal later (a new one) no longer learns '
                   'that those older messages were delivered or read.'),
    CompactionRule('location-tracks', 'Location tracks', frozenset((LOCATION_CONTENT_TYPE,)), frozenset(('location_update', 'meeting_update')), timedelta(days=7),
                   'Every position sent while a location is shared is an entry of its own (location_update, '
                   'meeting_update). Removed, the start and the end of each share stay: a device that downloads '
                   'the journal later shows those older shares without their track. Positions whose action '
                   'cannot be read without decrypting are left alone.'),
)


def compaction_rule(name):
    return next((rule for rule in COMPACTION_RULES if rule.name == name), None)


class JournalScanError(Exception):
    pass


def journal_page_url(history_url, cursor=None, since=None, content_types=None):
    """The journal page after `cursor`, or the first one from `since` (a datetime) on; with
    content_types, only the entries of those types (SylkServer's content_type argument; an older
    server ignores it and sends all of them)."""
    from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
    base = str(history_url)
    arguments = []
    if cursor:
        url = urljoin(f'{base.rstrip("/")}/', str(cursor))
    else:
        url = base
        if since is not None:
            arguments.append(('since', _utc_iso(since)))
    arguments += [('content_type', content_type) for content_type in sorted(content_types or ())]
    scheme, netloc, path, _, fragment = urlsplit(url)
    return urlunsplit((scheme, netloc, quote(path), urlencode(arguments, safe=':/'), fragment))


def _entry_time(value):
    """A journal timestamp as an aware UTC datetime, or None."""
    if not value:
        return None
    text = str(value).strip().replace('Z', '+00:00')
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def _entry_contact(value):
    """The other party of an entry as one spelling for all its entries: lower case, without sip:."""
    text = str(value or '').strip().lower()
    for scheme in ('sips:', 'sip:'):
        if text.startswith(scheme):
            text = text[len(scheme):]
    return text


def _entry_action(content_type, message):
    """The action of a location entry, read from its cleartext envelope (nothing is decrypted); '' else."""
    if content_type != LOCATION_CONTENT_TYPE:
        return ''
    try:
        from blink.location import storage_fields
        return str(storage_fields(message.get('content') or '', message.get('metadata')).get('related_action') or '')
    except Exception:
        return ''


def compaction_content_types(rules=COMPACTION_RULES):
    """The content types the rules look at: all a compaction needs to read of the journal."""
    return frozenset(content_type for rule in rules for content_type in rule.content_types)


def scan_journal(history_url, token, since=None, verify=True, timeout=(15, 60), progress=None, stop=None, max_pages=100000, content_types=None, stats=None):
    """Every entry of the account's journal from `since` (default JOURNAL_SINCE_YEARS ago) on, as
    JournalEntry; with content_types only those of these types (asked of the server, and checked here
    for a server that does not know the argument); progress(pages, entries) after each page; stop() returning True ends the walk early
    (what was read so far is returned). Raises JournalRemoveRefused for a refused token, JournalScanError else.
    stats, a dict, is filled with what the walk did: first_url, pages, received (entries the server
    sent), kept, and server_filtered (False when the server sent types not asked for: it does not
    know the content_type argument)."""
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=365 * JOURNAL_SINCE_YEARS)
    headers = {'Authorization': f'Apikey {token}'}
    entries = []
    cursor = None
    wanted = {str(content_type).lower() for content_type in content_types} if content_types else None
    stats = stats if stats is not None else {}
    stats.update(first_url=journal_page_url(history_url, None, since, content_types), pages=0, received=0, kept=0, server_filtered=True)
    for pages in range(1, max_pages + 1):
        if stop is not None and stop():
            break
        url = journal_page_url(history_url, cursor, since, content_types)
        try:
            response = requests.get(url, headers=headers, timeout=timeout, verify=verify)
        except requests.RequestException as e:
            raise JournalScanError(f'cannot reach {url}: {e}')
        if response.status_code == 401:
            raise JournalRemoveRefused('the server refused the API token (HTTP 401)')
        if response.status_code != 200:
            raise JournalScanError(f'HTTP {response.status_code} from {url}')
        try:
            data = response.json()
        except ValueError:
            raise JournalScanError(f'{url} did not answer JSON')
        messages = data.get('messages') if isinstance(data, dict) else None
        if not messages:
            break
        stats['pages'] = pages
        stats['received'] += len(messages)
        for message in messages:
            if isinstance(message, dict) and message.get('message_id'):
                content_type = str(message.get('content_type') or '').strip().lower() or '(none)'
                entries.append(JournalEntry(str(message['message_id']), _entry_time(message.get('timestamp')), content_type, _entry_action(content_type, message),
                                            _entry_contact(message.get('contact'))))
        if progress is not None:
            progress(pages, len(entries))
        if wanted is not None:
            if any(entry.content_type not in wanted for entry in entries):
                stats['server_filtered'] = False
                entries = [entry for entry in entries if entry.content_type in wanted]
        stats['kept'] = len(entries)
        last = messages[-1].get('message_id') if isinstance(messages[-1], dict) else None
        if not last or last == cursor:
            break
        cursor = last
    return entries


def parse_age(text):
    """'30d', '12w', '6m', '1y' (or a number of days) as a timedelta; '0' or 'all' for none."""
    text = str(text).strip().lower()
    if text in ('', '0', 'all'):
        return timedelta(0)
    match = re.fullmatch(r'(\d+)\s*([dwmy]?)', text)
    if not match:
        raise ValueError(f'not an age: {text!r} (use 30d, 12w, 6m or 1y)')
    number, unit = int(match.group(1)), match.group(2) or 'd'
    return timedelta(days=number * {'d': 1, 'w': 7, 'm': 30, 'y': 365}[unit])


def compaction_entries(entries, rule, older_than=None, now=None, contact=None):
    """The entries a rule removes: of its content types (and actions), older than `older_than`
    (default the rule's age; 0: all of them), of one contact when given. An entry without a
    readable timestamp is left alone unless all are asked for."""
    older_than = rule.age if older_than is None else older_than
    cutoff = (now or datetime.now(timezone.utc)) - older_than if older_than else None
    contact = _entry_contact(contact) if contact else None
    return [entry for entry in entries
            if entry.content_type in rule.content_types and (rule.actions is None or entry.action in rule.actions)
            and (cutoff is None or (entry.timestamp is not None and entry.timestamp < cutoff))
            and (contact is None or entry.contact == contact)]


def compaction_ids(entries, rule, older_than=None, now=None, contact=None):
    """The ids of the entries a rule removes (compaction_entries)."""
    return [entry.message_id for entry in compaction_entries(entries, rule, older_than, now, contact)]


def contact_breakdown(entries, rules, now=None):
    """What each rule removes, contact by contact: rules is [(rule, older_than)]; returns
    [(contact, {rule name: count}, total)], the contact with the most first."""
    now = now or datetime.now(timezone.utc)
    counts = {}
    for rule, older_than in rules:
        for entry in compaction_entries(entries, rule, older_than, now):
            per_rule = counts.setdefault(entry.contact, {})
            per_rule[rule.name] = per_rule.get(rule.name, 0) + 1
    return sorted(((contact, per_rule, sum(per_rule.values())) for contact, per_rule in counts.items()), key=lambda item: (-item[2], item[0]))


def journal_summary(entries, older_than=timedelta(0), now=None):
    """[(content type, entries, of them older than `older_than`, oldest, newest)], the most entries first."""
    cutoff = (now or datetime.now(timezone.utc)) - older_than if older_than else None
    summary = {}
    for entry in entries:
        total, old, oldest, newest = summary.get(entry.content_type, (0, 0, None, None))
        when = entry.timestamp
        if cutoff is None or (when is not None and when < cutoff):
            old += 1
        if when is not None:
            oldest = when if oldest is None or when < oldest else oldest
            newest = when if newest is None or when > newest else newest
        summary[entry.content_type] = (total + 1, old, oldest, newest)
    return sorted(((kind,) + values for kind, values in summary.items()), key=lambda item: (-item[1], item[0]))


def purge_journal(history_url, token, message_ids, verify=True, progress=None, stop=None, request_id=None):
    """Remove these ids from the journal, REMOVE_BATCH per request; returns (removed, requested).
    progress(done, removed, total) after each batch; stop() returning True ends it between batches.
    JournalRemoveError (or a subclass) is raised as remove_from_journal raises it."""
    message_ids = list(dict.fromkeys(str(message_id) for message_id in message_ids if message_id))
    request_id = request_id or str(uuid.uuid4())
    removed = done = 0
    for start in range(0, len(message_ids), REMOVE_BATCH):
        if stop is not None and stop():
            break
        batch = message_ids[start:start + REMOVE_BATCH]
        removed += len(remove_from_journal(history_url, token, batch, verify=verify, request_id=request_id))
        done += len(batch)
        if progress is not None:
            progress(done, removed, len(message_ids))
    return removed, done


# Bulk delete on the other devices
#
# One application/sylk-messages-bulk-delete message carries the criteria of a deletion made in the
# Data manager; every device of the account that receives it (live, or later from the journal)
# erases what matches in its own history. See docs/messaging/messages-bulk-delete.md.

BULK_DELETE_VERSION = 1

# Categories a criterion may name: the category column every client fills the same way. Location
# sharing is named by its content type instead, so its starts (category location) and its ticks
# (no category) match alike on every client.
BULK_DELETE_CATEGORIES = ('text', 'image', 'audio', 'video', 'other', 'call')

def _utc_iso(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{value.microsecond // 1000:03d}Z'


def period_bounds(period):
    """(from, until) in UTC for a local 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'; (None, None) for None."""
    if not period:
        return None, None
    parts = [int(part) for part in period.split('-')]
    year, month, day = (parts + [1, 1])[:3]
    start = datetime(year, month, day)
    if len(parts) == 1:
        end = datetime(year + 1, 1, 1)
    elif len(parts) == 2:
        end = datetime(year + (month == 12), month % 12 + 1, 1)
    else:
        end = start + timedelta(days=1)
    return _utc_iso(start.astimezone()), _utc_iso(end.astimezone())


def bulk_delete_payload(kind, action, contact, period, device_id, files=True, cascade=True, bulk_delete_id=None, now=None):
    """The payload announcing a deletion made with these Data manager filters: whatever the user
    selected is the criteria, 'encrypted' (the action cannot be read on that device) included. None
    without a type, a contact and a period: a bulk delete needs all three."""
    if kind is None or not contact or not period:
        return None
    criteria = {}
    if kind is not None:
        if kind == 'location':
            criteria['content_type'] = LOCATION_CONTENT_TYPE
        elif kind in BULK_DELETE_CATEGORIES:
            criteria['category'] = kind
        elif kind in CATEGORY_LABELS:
            return None
        else:
            criteria['content_type'] = kind
        if action is not None and 'content_type' in criteria:
            criteria['action'] = action
    if contact:
        criteria['contact'] = contact
    now = now or datetime.now(timezone.utc)
    start, end = period_bounds(period)
    criteria['from'] = start
    criteria['until'] = min(end, _utc_iso(now))
    return {'v': BULK_DELETE_VERSION,
            'bulk_delete_id': bulk_delete_id or str(uuid.uuid4()),
            'device_id': device_id,
            'timestamp': _utc_iso(now),
            'criteria': criteria,
            'files': bool(files),
            'cascade': bool(cascade)}


def bulk_delete_criteria(payload):
    """The criteria of a received bulk delete, checked, or None (ignored). A type, a contact
    and the period (`from` and `until`) are required."""
    if not isinstance(payload, dict) or not isinstance(payload.get('criteria'), dict):
        return None
    criteria = payload['criteria']
    if 'ids' in criteria:
        # devices share criteria, never lists of message ids
        return None
    category, content_type = criteria.get('category'), criteria.get('content_type')
    if category and content_type:
        return None
    if category and category not in BULK_DELETE_CATEGORIES:
        return None
    if criteria.get('action') and not content_type:
        return None
    if not (category or content_type) or not criteria.get('contact'):
        return None
    if not (criteria.get('from') and criteria.get('until')):
        return None
    result = {}
    for name in ('category', 'content_type', 'action', 'contact', 'from', 'until'):
        if criteria.get(name):
            result[name] = str(criteria[name]).strip()
    if 'content_type' in result:
        result['content_type'] = result['content_type'].lower()
    return result


def criteria_matches_action(criteria, category, content_type, related_action, head):
    """Whether a row selected by type, contact and time also has the criteria's action."""
    if not criteria.get('action'):
        return True
    kind, action = classify(category, content_type, related_action, head)
    return action == criteria['action']


def criteria_label(criteria):
    """The criteria as the Data manager would name them, for the log."""
    if criteria.get('category'):
        what = type_label(criteria['category'])
    elif criteria.get('content_type'):
        kind = 'location' if criteria['content_type'] == LOCATION_CONTENT_TYPE else criteria['content_type']
        what = type_label(kind) + (f' · {criteria["action"]}' if criteria.get('action') else '')
    else:
        what = 'All types'
    parts = [what, criteria.get('contact') or 'all contacts']
    parts.append(f'{criteria.get("from") or "the beginning"} .. {criteria["until"]}')
    return ', '.join(parts)


# File transfers whose file is on the server and not on this computer
#
# A transfer's file is here when one of its folders holds it -- file_transfers/<account>/<peer>/<id>/,
# or downloads/<id>/ before that layout -- the decrypted file for an .asc, under any of the ids the
# message is known by: the envelope's transfer_id, the URL's id segment, the message id. That is what
# blink.messagepane.files.local_file looks for one message at a time; here the storage is listed once
# for the whole history. A transfer is on the server when it has a URL, has not passed its `until`,
# and was not answered 404/410 before (.failure.json of kind gone).

FILE_KINDS = ('image', 'audio', 'video', 'other')

ON_SERVER = 'on_server'     # not fetched yet
FAILED = 'failed'           # fetching failed before (.failure.json, not gone): Download asks again
NO_KEY = 'no_key'           # encrypted (.asc) and the account has no private key to open it

DOWNLOADABLE = (ON_SERVER, FAILED)

STATUS_LABELS = {ON_SERVER: 'On the server', FAILED: 'Failed before', NO_KEY: 'Encrypted, no key here'}

# 'type' and 'action' (the status) so that select, count_by and aggregate_days drill into them as into messages
FileRow = namedtuple('FileRow', 'id message_id contact day timestamp direction type action name wire_name size filetype url until reason ids')

_FILE_EXTENSIONS = {'image': ('jpg', 'jpeg', 'png', 'gif', 'heic', 'heif', 'webp', 'tiff', 'bmp'),
                    'video': ('mp4', 'mov', 'm4v', 'avi', 'mkv', 'webm', '3gp'),
                    'audio': ('mp3', 'm4a', 'wav', 'aac', 'ogg', 'opus', 'caf', 'flac')}


def status_label(status):
    if status is None:
        return 'Any status'
    return STATUS_LABELS.get(status, status)


def scan_transfer_storage(file_transfers_root, downloads_root):
    """{transfer folder name: (set of file names, failure dict or None)} of everything stored, partial
    downloads (.download) left out. A name in several peer folders has the union of their files."""
    storage = {}

    def add(path, transfer_id):
        names = set()
        failure = None
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name == '.failure.json':
                        try:
                            with open(entry.path) as file:
                                failure = json.load(file)
                        except (OSError, ValueError):
                            failure = None
                        if not isinstance(failure, dict):
                            failure = {}
                    elif not entry.name.endswith('.download') and entry.is_file():
                        names.add(entry.name)
        except OSError:
            return
        known = storage.get(transfer_id)
        if known is not None:
            names |= known[0]
            failure = known[1] if known[1] is not None else failure
        storage[transfer_id] = (names, failure)

    def folders(path):
        try:
            with os.scandir(path) as entries:
                return [entry for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    for account in folders(file_transfers_root):
        for peer in folders(account.path):
            for transfer in folders(peer.path):
                add(transfer.path, transfer.name)
    for transfer in folders(downloads_root):
        add(transfer.path, transfer.name)
    return storage


def _url_id(url):
    # <base>/<sender>/<receiver>/<transfer id>/<name>
    parts = str(url or '').split('?', 1)[0].rstrip('/').split('/')
    return parts[-2] if len(parts) >= 5 else None


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


def _file_kind(category, filetype, name):
    if category in FILE_KINDS:
        return category
    filetype = str(filetype or '').lower()
    for kind in ('image', 'video', 'audio'):
        if filetype.startswith(kind + '/'):
            return kind
    extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    for kind, extensions in _FILE_EXTENSIONS.items():
        if extension in extensions:
            return kind
    return 'other'


def build_file_rows(db_rows, storage, can_decrypt=True, now=None):
    """(rows, counts) from MessageHistory.data_file_transfers tuples and scan_transfer_storage: rows are
    the transfers whose file is not here and is on the server; counts tells what became of the others:
    {'transfers', 'here', 'expired', 'gone', 'no_url', 'unreadable'}."""
    now = now or datetime.now(timezone.utc)
    counts = Counter(transfers=0, here=0, expired=0, gone=0, no_url=0, unreadable=0)
    rows = []
    for row_id, message_id, remote_uri, day, timestamp, direction, category, content in db_rows:
        counts['transfers'] += 1
        meta = file_transfer_envelope(content) if content else None
        if not meta or not meta.get('filename') or not message_id:
            counts['unreadable'] += 1       # an envelope nobody decrypted, or not one at all
            continue
        wire_name = transfer_filename(meta['filename'])
        name = wire_name[:-4] if wire_name.endswith('.asc') else wire_name
        ids = []
        for transfer_id in (meta.get('transfer_id'), _url_id(meta.get('url')), message_id):
            if transfer_id and safe_component(transfer_id) not in ids:
                ids.append(safe_component(transfer_id))
        failure = None
        here = False
        for transfer_id in ids:
            found = storage.get(transfer_id)
            if found is None:
                continue
            if name in found[0]:
                here = True
                break
            if failure is None:
                failure = found[1]
        if here:
            counts['here'] += 1
            continue
        if not meta.get('url'):
            counts['no_url'] += 1
            continue
        if _expired(meta.get('until'), now):
            counts['expired'] += 1
            continue
        if failure is not None and failure.get('kind') == FAILURE_GONE:
            counts['gone'] += 1
            continue
        if failure is not None:
            status, reason = FAILED, str(failure.get('reason') or 'failed')
        elif wire_name.endswith('.asc') and not can_decrypt:
            status, reason = NO_KEY, 'encrypted, and this account has no private key here'
        else:
            status, reason = ON_SERVER, ''
        try:
            size = int(meta.get('filesize') or 0) or None
        except (TypeError, ValueError):
            size = None
        rows.append(FileRow(int(row_id), str(message_id), str(remote_uri or ''), str(day or ''), str(timestamp or ''), str(direction or ''),
                            _file_kind(category, meta.get('filetype'), name), status, name, str(meta['filename']), size, str(meta.get('filetype') or ''),
                            str(meta['url']), meta.get('until'), reason, tuple(ids)))
    return rows, dict(counts)
