"""The message info panel: everything known about one message, stored and shown.

Sections: Message (ids, parties, account, time as stored and as sent), Delivery
(state, disposition asked for, read), Replies (what it answers, what answers it),
File transfer (name, size, type, URL, where it is here, a failure), Location
(the share's fields), Storage (the history row's columns as stored) and Related
(rows filed against it: links, captions, waveforms, trail ticks). The values are
read from history in the db thread when the panel opens, so it shows the
stored state next to what the transcript shows. All text can be selected.
"""

import html
import json

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

from sipsimple.threading import run_in_thread

from blink.util import call_in_gui_thread, translate


__all__ = ['show_message_info']


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
    call_in_gui_thread(_show, parent, item, shown, stored, related_rows, answers)


def _detail(row, label_metadata, reply_metadata):
    if row.related_action == 'reply':
        link = reply_metadata(row.content)
        return f'answers {link["original_id"]}' if link else ''
    if row.related_action == 'label':
        label = label_metadata(row.content)
        return f'caption {label["label"]!r}' if label else ''
    return ''


def _section(title, pairs):
    rows = ''.join(f'<tr><td style="color:gray;padding-right:12px;vertical-align:top;white-space:nowrap">{html.escape(str(key))}</td>'
                   f'<td style="white-space:pre-wrap">{html.escape(str(value))}</td></tr>'
                   for key, value in pairs if value not in (None, '', [], {}))
    return f'<h3 style="margin-top:14px">{html.escape(title)}</h3><table cellspacing="2">{rows}</table>' if rows else ''


def _show(parent, item, shown, stored, related_rows, answers):
    from blink.message_envelopes import file_transfer_envelope
    from blink.messagepane.files import failure_reason, local_file
    row = stored[0] if stored else {}
    parts = []
    parts.append(_section(translate('message_info', 'Message'), [
        ('Message id', item.id),
        ('Direction', item.direction),
        ('Conversation', item.remote_uri),
        ('Party', item.uri),
        ('Name', item.display_name),
        ('Account', item.account_id),
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
    storage = [(column, value) for column, value in row.items() if column not in ('content',)]
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
