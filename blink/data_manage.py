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
import re
import uuid

from collections import Counter, namedtuple
from datetime import datetime, timedelta, timezone

import requests

from blink.message_envelopes import LOCATION_CONTENT_TYPE, METADATA_CONTENT_TYPE


__all__ = ['with_related', 'period_bounds', 'bulk_delete_payload', 'bulk_delete_criteria', 'criteria_matches_action', 'criteria_label', 'BULK_DELETE_CATEGORIES',
           'Row', 'ENCRYPTED', 'classify', 'type_label', 'action_label', 'row_label', 'build_rows', 'select', 'count_by', 'aggregate_days',
           'REMOVE_BATCH', 'PROGRESS_EVERY', 'JournalRemoveError', 'JournalRemoveUnsupported', 'JournalRemoveRefused', 'journal_remove_url', 'remove_from_journal']


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
