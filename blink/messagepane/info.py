"""The message info panel: everything known about one message, stored and shown.

Sections: Message (ids, parties, account, time as stored and as sent), Delivery
(state, disposition asked for, read), Clients (the client that sent it and the ones
that sent its receipts, from blink.history.MessageAgent), Replies (what it answers, what answers it),
File transfer (name, size, type, URL, where it is here, a failure), Location
(the share's fields), Storage (the history row's columns as stored) and Related
(rows filed against it: links, captions, waveforms, trail ticks). The values are
read from history in the db thread when the panel opens, so it shows the
stored state next to what the transcript shows. All text can be selected.
"""

import html
import json

from datetime import timezone

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

from sipsimple.threading import run_in_thread

from blink.util import call_in_gui_thread, translate


__all__ = ['show_message_info', 'show_call_details']


_open_panels = []


def show_message_info(parent, item, shown):
    """Open the panel for a MessageItem; shown: {label: value} of what the transcript draws."""
    _load(parent, item, shown)


@run_in_thread('db')
def _load(parent, item, shown):
    from blink.history import Message, MessageHistory
    from blink.message_envelopes import label_metadata, reply_metadata
    rows = list(Message.selectBy(message_id=item.id))
    related = MessageHistory().related_messages([item.id])
    table = Message.sqlmeta.table
    db = Message._connection
    answers = []
    try:
        for (content, reply_id) in db.queryAll(f"select content, related_msg_id from {table} where related_action = 'reply' and content like {db.sqlrepr('%' + item.id + '%')}"):
            link = reply_metadata(content)
            if link is not None and link['original_id'] == item.id:
                answers.append(link['reply_id'])
    except Exception:
        pass
    stored = []
    for row in rows:
        stored.append({column: getattr(row, column, None) for column in ['id'] + list(row.sqlmeta.columns)})
    related_rows = [{'message_id': row.message_id, 'action': row.related_action, 'content_type': row.content_type,
                     'time': str(row.timestamp), 'deleted': row.deleted, 'detail': _detail(row, label_metadata, reply_metadata)} for row in related]
    agents = MessageHistory().agents(item.id)
    call_in_gui_thread(_show, parent, item, shown, stored, related_rows, answers, agents)


_AGENT_LABELS = {'sent': 'Sent from', 'delivered': 'Delivered to', 'displayed': 'Displayed on', 'error': 'Error from',
                 'failed': 'Failed on', 'forbidden': 'Refused by', 'processed': 'Processed by', 'stored': 'Stored by'}


def _agent_rows(agents):
    """(label, "client, via relay, at time") of who sent a message and who answered it."""
    rows = []
    for kind, user_agent, relay, received_at in agents:
        when = received_at.replace(tzinfo=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S') if received_at else ''
        text = user_agent + (f'\nvia {relay}' if relay else '') + (f'\nat {when}' if when else '')
        rows.append((_AGENT_LABELS.get(kind, kind), text))
    return rows


def _sent_by(agents):
    """The client that sent the message: as it said (this device's own, for one sent from here), else why it is not known."""
    for kind, user_agent, relay, received_at in agents:
        if kind == 'sent':
            return user_agent + (f' (via {relay})' if relay else '')
    return translate('message_info', 'not known (fetched from the journal, or received before user agents were kept)')


def _detail(row, label_metadata, reply_metadata):
    if row.related_action == 'reply':
        link = reply_metadata(row.content)
        return f'answers {link["original_id"]}' if link else ''
    if row.related_action == 'label':
        label = label_metadata(row.content)
        return f'caption {label["label"]!r}' if label else ''
    return ''


def _plain(value):
    """A value as people read it: lists (and lists stored as their text) comma separated, addresses without sip:."""
    if isinstance(value, str) and value.startswith('[') and value.endswith(']'):
        import ast
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            value = parsed
    if isinstance(value, (list, tuple, set)):
        return ', '.join(_plain(part) for part in value)
    text = str(value)
    for scheme in ('sips:', 'sip:'):
        if text.lower().startswith(scheme):
            return text[len(scheme):]
    return text


class _Link(object):
    """A value drawn as a link that opens in the browser (http and https only)."""
    def __init__(self, url, text=None):
        self.url = url
        self.text = text or url


def _cell(value):
    if isinstance(value, _Link):
        return f'<a href="{html.escape(value.url, quote=True)}" title="{html.escape(value.url, quote=True)}">{html.escape(value.text)}</a>'
    return html.escape(_plain(value))


def _section(title, pairs):
    rows = ''.join(f'<tr><td style="color:gray;padding-right:12px;vertical-align:top;white-space:nowrap">{html.escape(str(key))}</td>'
                   f'<td style="white-space:pre-wrap">{_cell(value)}</td></tr>'
                   for key, value in pairs if isinstance(value, _Link) or value not in (None, '', [], {}, (), '[]'))
    return f'<h3 style="margin-top:14px">{html.escape(title)}</h3><table cellspacing="2">{rows}</table>' if rows else ''


def _show(parent, item, shown, stored, related_rows, answers, agents=()):
    from blink.message_envelopes import file_transfer_envelope
    from blink.messagepane.files import failure_reason, local_file
    row = stored[0] if stored else {}
    if item.category == 'call' or item.content_type in ('application/blink-call-detail-record', 'application/blink-call-history'):
        from blink.message_envelopes import call_record
        record = call_record(item.content, getattr(item, 'metadata', None) or row.get('metadata'))
        if record:
            # a call detail record: only what the call was, not the row it is stored in
            _dialog(parent, translate('message_info', 'Message Info'), _call_sections(record)[0])
            return
    parts = []
    parts.append(_section(translate('message_info', 'Message'), [
        ('Message id', item.id),
        ('Direction', item.direction),
        ('Conversation', item.remote_uri),
        ('Party', item.uri),
        ('Name', item.display_name),
        ('Account', item.account_id),
        ('User agent', _sent_by(agents)),
        ('Time', item.timestamp.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')),
        ('Sent at (as received)', row.get('cpim_timestamp')),
        ('Content type', item.content_type),
        ('Category', item.category),
        ('Encryption', item.encryption_type),
    ]))
    parts.append(_section(translate('message_info', 'Delivery'), [
        ('State', item.state),
        ('Disposition asked for', item.disposition),
        ('Read', {1: 'yes', 0: 'no'}.get(item.read, item.read)),
        ('Decrypted', {'1': 'yes', '0': 'no'}.get(str(item.decrypted), item.decrypted)),
    ]))
    parts.append(_section(translate('message_info', 'Clients'), _agent_rows(agents)))
    reply = item.reply
    parts.append(_section(translate('message_info', 'Replies'), [
        ('Answers', f"{reply['id']}: {reply['text']}" if reply else ''),
        ('Answered by', ', '.join(answers)),
    ]))
    meta = file_transfer_envelope(item.content) if item.category in ('image', 'audio', 'video', 'other') and item.content else None
    if meta:
        parts.append(_section(translate('message_info', 'File transfer'), [
            ('Name', meta.get('filename')), ('Size', meta.get('filesize')), ('Type', meta.get('filetype')),
            ('URL', meta.get('url')), ('Available until', meta.get('until')), ('Here', local_file(item) or 'not downloaded'),
            ('Failure', failure_reason(item)), ('Caption', item.caption),
        ]))
    if item.category == 'location':
        try:
            body = json.loads(item.content) if item.content else {}
        except (TypeError, ValueError):
            body = {}
        parts.append(_section(translate('message_info', 'Location'), [(key, body[key]) for key in sorted(body) if not isinstance(body[key], (dict, list))] +
                              [('Metadata', item.metadata)]))
    parts.append(_section(translate('message_info', 'Shown'), list(shown.items())))
    storage = [(column, _pretty_json(value) if column == 'metadata' else value) for column, value in row.items() if column not in ('content',)]
    if row.get('content') is not None:
        content = row['content']
        storage.append(('content', content if len(content) <= 4000 else content[:4000] + f'… ({len(content)} characters)'))
    if len(stored) > 1:
        storage.append(('Rows', f'{len(stored)} (filed under: {", ".join(str(other.get("account_id")) for other in stored)})'))
    parts.append(_section(translate('message_info', 'Storage'), storage or [('Row', 'not stored')]))
    for number, related in enumerate(related_rows, 1):
        parts.append(_section(translate('message_info', 'Related %d') % number, list(related.items())))

    dialog = QDialog(parent)
    dialog.setWindowTitle(translate('message_info', 'Message Info'))
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(560, 640)
    layout = QVBoxLayout(dialog)
    browser = QTextBrowser(dialog)
    browser.setOpenLinks(False)
    browser.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    browser.setHtml(''.join(parts))
    layout.addWidget(browser)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    _open_panels.append(dialog)
    dialog.destroyed.connect(lambda *args, dialog=dialog: _open_panels.remove(dialog) if dialog in _open_panels else None)
    dialog.show()


_CALL_FIELDS = (('direction', 'Direction'), ('outcome', 'Outcome'), ('duration', 'Duration'), ('status', 'SIP status'), ('reason', 'Reason'),
                ('remoteParty', 'Remote party'), ('displayName', 'Name'), ('startTime', 'Started'), ('stopTime', 'Ended'),
                ('timezone', 'Time zone'), ('media', 'Media'), ('answeredBy', 'Answered by device'), ('source', 'Recorded by'),
                ('sessionId', 'Call-ID'), ('fromTag', 'From tag'), ('toTag', 'To tag'), ('proxyIP', 'Proxy'), ('sipTraceUrl', 'SIP trace'))


_CALL_WORDS = {'incoming': 'Incoming', 'outgoing': 'Outgoing', 'completed': 'Completed', 'missed': 'Missed',
               'cancelled': 'Cancelled', 'failed': 'Failed', 'answered_elsewhere': 'Answered on another device',
               'busy': 'Busy', 'declined': 'Declined', 'local': 'This device', 'server': 'Server', 'remote': 'Another device'}

_CALL_SUBKEYS = {'deviceId': 'Device id', 'streams': 'Streams', 'userAgent': 'Client', 'answeredBy': 'Answered by device'}


def _call_time(value):
    """An ISO time of a call record in local time, as people read it."""
    if not value:
        return value
    try:
        from dateutil.parser import parse
        when = parse(str(value))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
    except (ValueError, OverflowError, TypeError):
        return value


def _call_value(field, value):
    from blink.message_envelopes import format_call_duration
    if value in (None, '', [], {}):
        return None
    if field == 'duration':
        return format_call_duration(value) or f'{value} s'
    if field in ('startTime', 'stopTime'):
        return _call_time(value)
    if field == 'sipTraceUrl':
        url = str(value).strip()
        # the record is synced between devices: only a web link is offered to the browser
        return _Link(url, translate('call_details', 'Open SIP trace')) if url.lower().startswith(('https://', 'http://')) else url
    if field in ('direction', 'outcome', 'source') and isinstance(value, str):
        return _CALL_WORDS.get(value, value.replace('_', ' ').capitalize())
    if field == 'media':
        if isinstance(value, str) and value.lstrip().startswith('['):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        if isinstance(value, (list, tuple)):
            return ', '.join(_media_name(part) for part in value)
        return _media_name(value)
    return _readable(value)


def _media_name(part):
    """One media of a call record: a stream type, or a stream described by a dict."""
    if isinstance(part, dict):
        kind = part.get('type') or part.get('media') or part.get('kind')
        rest = ', '.join(f'{_title(key)} {value}' for key, value in part.items() if key not in ('type', 'media', 'kind') and value not in (None, ''))
        return f"{str(kind or '').capitalize()} ({rest})" if rest else str(kind or '').capitalize()
    return str(part).capitalize()


def _readable(value, indent=''):
    """Nested record values as indented lines of text instead of JSON."""
    if isinstance(value, dict):
        lines = []
        for key, part in value.items():
            if part is None or part == '' or part == [] or part == {}:
                continue
            label = _CALL_SUBKEYS.get(key, _title(key))
            text = str(_readable(part, indent + '    '))
            if isinstance(part, dict) or '\n' in text:
                lines.append(f'{indent}{label}:')
                lines.append(text if isinstance(part, dict) else '\n'.join(indent + '    ' + line.lstrip() for line in text.split('\n')))
            else:
                lines.append(f'{indent}{label}: {text}')
        return '\n'.join(lines)
    if isinstance(value, (list, tuple)):
        if all(isinstance(part, (str, int, float, bool)) for part in value):
            return ', '.join(str(part) for part in value)
        return '\n'.join(str(_readable(part, indent)) for part in value)
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    return value


def _title(key):
    """camelCase or snake_case key as words."""
    import re
    if not re.fullmatch(r'[A-Za-z_]+', str(key)):
        return str(key)         # an id (a device, a stream), not a field name
    words = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', str(key)).replace('_', ' ').strip()
    return words[:1].upper() + words[1:].lower()


def _pretty_json(value):
    """A JSON text indented for reading, a list of plain values on one line; anything else unchanged."""
    if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            return _json_lines(json.loads(value))
        except (TypeError, ValueError):
            pass
    return value


def _json_lines(value, indent=''):
    inner = indent + '  '
    if isinstance(value, dict):
        if not value:
            return '{}'
        items = [f'{inner}{json.dumps(key)}: {_json_lines(value[key], inner)}' for key in sorted(value)]
        return '{\n' + ',\n'.join(items) + '\n' + indent + '}'
    if isinstance(value, list):
        if all(not isinstance(part, (dict, list)) for part in value):
            return '[' + ', '.join(json.dumps(part) for part in value) + ']'
        return '[\n' + ',\n'.join(inner + _json_lines(part, inner) for part in value) + '\n' + indent + ']'
    return json.dumps(value)


def _call_sections(record):
    """The call record of a call row as readable sections: the call, this device's part,
    and any other part of the record (merged views of other devices, the server's)."""
    from blink.message_envelopes import call_summary, this_device_id
    known = {field for field, title in _CALL_FIELDS} | {'version', 'local'}
    pairs = [(translate('call_details', 'Summary'), call_summary(record, this_device_id()))]
    pairs += [(translate('call_details', title), _call_value(field, record.get(field))) for field, title in _CALL_FIELDS]
    pairs += [(_title(key), _readable(value)) for key, value in sorted(record.items())
              if key not in known and not isinstance(value, dict)]
    sections = [_section(translate('call_details', 'Call'), pairs)]
    local = record.get('local') if isinstance(record.get('local'), dict) else {}
    sections.append(_section(translate('call_details', 'This device'),
                             [(_CALL_SUBKEYS.get(key, _title(key)), _readable(value)) for key, value in sorted(local.items())]))
    for key, value in sorted(record.items()):
        if key not in known and isinstance(value, dict):
            sections.append(_section(_title(key), [(_CALL_SUBKEYS.get(sub, _title(sub)), _readable(part)) for sub, part in sorted(value.items())]))
    return sections


def show_call_details(parent, item, record):
    """The call details dialog: what the call record says, field by field (selectable)."""
    if not record:
        show_message_info(parent, item, {'Drawn as': 'call'})
        return
    parts = _call_sections(record)
    parts.append(_section(translate('call_details', 'Record'), [('JSON', _json_lines(record))]))
    _dialog(parent, translate('call_details', 'Call Details'), ''.join(parts))


def _open_link(url):
    from PyQt6.QtGui import QDesktopServices
    if url.scheme() in ('http', 'https'):
        QDesktopServices.openUrl(url)


def _dialog(parent, title, body):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(560, 640)
    layout = QVBoxLayout(dialog)
    browser = QTextBrowser(dialog)
    browser.setOpenLinks(False)
    browser.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    browser.setHtml(body)
    browser.anchorClicked.connect(_open_link)
    layout.addWidget(browser)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    _open_panels.append(dialog)
    dialog.destroyed.connect(lambda *args, dialog=dialog: _open_panels.remove(dialog) if dialog in _open_panels else None)
    dialog.show()
