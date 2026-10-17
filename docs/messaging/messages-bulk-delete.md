# Bulk deletion of messages

A user deletes messages in bulk in the Data manager of a client. The client
selects them by criteria: type, action, contact and period. The deletion then
reaches three places:

1. **This device.** The matching rows are erased, with no tombstones.
2. **The journal on the server.** The ids are removed with
   `POST <journal url>/remove`, so a new device no longer downloads them.
3. **The other devices of the account.** One
   `application/sylk-messages-bulk-delete` message carries the criteria. Every
   device that receives it erases what matches in its own history.

Step 3 does not depend on step 2. The user asked for these messages to go, so
the other devices are told even when the server removal failed or was not
asked for.

## The Data manager (sender)

Blink → Accounts → Data manager. It loads the enabled accounts of the current
profile only.

- **Filters** narrow each other in any order and show counts: Type, Action
  (under location sharing and the uncategorized types), Contact, Year → Month
  → Day in local time. Reset filters starts over.
- **Delete** takes the whole selection the filters make, with the rows filed
  against it (cascade). It is enabled only when a type, a contact and a
  period are chosen. There is no confirmation dialog.
- **Also remove them from the journal on the server** (checked by default)
  removes the ids from the journal. Only for accounts with a SylkServer
  journal; other accounts (Bonjour, plain SIP) delete on this device only.
- **Also delete them from my other devices** (checked by default) sends the
  bulk delete message, first, before the server removal starts.
- **Order:** announce, then per batch of up to 1000 ids: remove from the
  journal, erase locally. Progress is shown as the server reports it. Stop
  ends the run; what was removed stays removed.
- **One message.** Clicking a row shows it in full with a **Delete This
  Message** button. That is an ordinary single delete
  (`sylk-api-message-remove` to the contact), not a bulk delete.

## 1. Removal from the journal (SylkServer)

```
POST /webrtcgateway/messages/history/<account>/remove
Authorization: Apikey <journal token>
Content-Type: application/json

{"ids": ["<message id>", ...], "progress": 100}
```

- A request carries at most 1000 ids. The server removes only this account's
  rows with those ids. It writes no tombstone, sends nothing to any device and
  leaves the peer's copy alone. Unlike `sylk-api-message-remove`, a location
  track is not removed as a whole: only the listed ids go.
- A transfer's file is stored once, under either party, and both parties'
  rows point to it. When a row of a file transfer is removed, its folder is
  deleted only if the other party's journal no longer has a row with that
  transfer id (or that party is not on this server). The second removal frees
  the space.
- Without `progress` the answer is one JSON object:
  `{"ok": true, "requested": N, "removed": [ids]}`.
- With `"progress": N` the answer is JSON lines (`application/x-ndjson`).
  While the server works it sends `{"done", "removed", "total"}` every N ids,
  and it ends with the object above. A client that disconnects stops the
  removal at the next id.
- A server without the endpoint answers 404 or 405. A refused token gets 401.

## 2. The bulk delete message

**Device → server.** The device sends `application/sylk-api-messages-bulk-delete`
to its own account, without CPIM and not encrypted. The server refuses one sent
from an account to another account.

**Server → devices.** The server stores the payload once in the account's
journal as `application/sylk-messages-bulk-delete`, with message id
`bulk_delete_id`, so offline devices get it at their next sync. It also sends
the payload live to every device of the account, the sender's included.

```json
{
  "v": 1,
  "bulk_delete_id": "6f1c2b8e-…",
  "device_id": "c359a142-…",
  "timestamp": "2026-10-11T11:37:12.000Z",
  "criteria": {
    "content_type": "application/sylk-message-metadata",
    "action": "location",
    "contact": "florig@sylk.link",
    "from": "2026-05-01T03:00:00.000Z",
    "until": "2026-06-01T03:00:00.000Z"
  },
  "files": true,
  "cascade": true
}
```

| Field | Meaning |
|---|---|
| `bulk_delete_id` | Unique id of this deletion; the journal entry's message id. |
| `device_id` | The device that deleted. It ignores its own echo. |
| `timestamp` | When the deletion was made, UTC. |
| `files` | Also erase the downloaded files of matching file transfers. |
| `cascade` | Also erase the rows filed against the matching ones by `related_msg_id`, all the way down: the ticks and stop of a location share, the reply, label, peaks and call recording sidecars of a message. |
| `criteria.category` | `text`, `image`, `audio`, `video`, `other` or `call`: the persisted category column, filled by the classifier every client ports from Sylk Mobile. |
| `criteria.content_type` | The wire content type, for rows without a category, e.g. `application/sylk-message-metadata`. Location sharing is always named this way (`application/sylk-location-sharing`), so the start of a share (category `location`) and its ticks (no category) match alike. |
| `criteria.action` | Only together with `content_type`. Location sharing: the envelope action (`location_start`, `location_update`, `location_stop`, `location_once`, `location_request`, `meeting_*`). Metadata: its `action` (`reply`, `label`, `peaks`, `consumed`, `rotation`, `location`, …). `encrypted`: the action cannot be read (PGP body, no stored action). |
| `criteria.contact` | Required. The other party, `user@domain`, matched against every spelling the receiver files that conversation under. |
| `criteria.from` | Message time ≥ this, UTC. Required. |
| `criteria.until` | Message time < this, UTC. Required: the end of the period, or the deletion's own `timestamp` if that is earlier. |

Rules for both sides:

- **Type, contact and period are all required.** Exactly one of `category`
  or `content_type`, plus `contact`, `from` and `until`. The Data manager does
  not delete until a type, a contact and a year, month or day are chosen, and
  a receiver ignores criteria missing any of them. A mistake can then only
  reach one kind of message, with one contact, in one period.
- **The sender converts the period to UTC.** It turns its local year, month or
  day into `from` and `until`, so devices in other time zones select the same
  messages.
- **Criteria only, never message ids.** The Data manager is a bulk tool: it
  always deletes the whole selection its filters make, and announces those
  filters. Single messages are deleted in the conversation, with
  `sylk-api-message-remove`. A receiver ignores a bulk delete whose criteria
  carry `ids`.
- **The filters as the user chose them.** `action: "encrypted"` is a criterion
  like any other: on each device it selects the rows of that type whose body is
  still PGP ciphertext and that have no stored action. A device that decrypted
  some of them selects fewer.

## 3. Applying it (receiver)

1. Ignore the message when `device_id` is this device's own.
2. Select the account's rows by `category` or `content_type`, `contact` and
   `from`/`until`. When `action` is given, keep the rows whose action matches.
   The action is the one stored with the row (`related_action`); if there is
   none, it is the `action` in the body. A row whose body is still PGP-armoured
   and that has no stored action does not match an action.
3. With `cascade`, add the rows whose `related_msg_id` is the message id of a
   selected row, and repeat for those, until nothing new is found.
4. Erase the selected rows: delete them, do not tombstone them. Keep nothing
   pending; a message that arrives later is newer than `until` or was removed
   from the journal. When `files` is set, erase the folders of the file
   transfers among them.
5. Refresh conversation previews and unread counts once. The deletion counts
   as nothing unread and notifies nobody.
6. Log one line, for example:
   `[data] Bulk delete of 2365 rows of ag@sylk.link (Metadata · location, florig@sylk.link, …) on request of <device>, live`.

The same deletion can reach a device twice: live, then again from the
journal. The second time it selects nothing, so it needs no bookkeeping.

## Implementations

- Blink Qt: `blink/data_manage.py` (payload, checks, matching),
  `blink/datawindow.py` (Data manager, sender), `blink/messages.py`
  (`send_messages_bulk_delete`, `apply_messages_bulk_delete`, live and
  journal), `blink/history.py` (`bulk_delete`).
- SylkServer: `webrtcgateway/web.py` (`/remove`),
  `webrtcgateway/storage.py` (`removeMessages`),
  `webrtcgateway/sip_handlers.py` (`_handle_messages_bulk_delete`).
- Sylk Mobile: receiving done; sending to do.
- Blink for macOS: receiving done (`HistoryManager.py` `bulk_delete`,
  `SMSWindowManager.py` `applyMessagesBulkDelete`); sending to do.
