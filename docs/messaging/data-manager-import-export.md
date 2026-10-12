# Data manager: Import from device and Export to device

Copying message history directly between devices of the same account over
the local network, without the server: from Sylk Mobile to Blink, from Blink
to Sylk Mobile, or between two Blinks. Blink speaks Sylk Mobile's own export
API (sylk-mobile `docs/export/Readme.md`), so both apps work against it
unchanged.

Blink > Accounts > Data manager, tabs **Import from device** and **Export to device**.

## Import from device

1. On the other device, start its data export (Sylk Mobile: main menu >
   Export data…; Blink: Data manager > Export to device).
2. It announces itself to the account: `application/sylk-data-export`,
   PGP-encrypted to the account's own public key, payload
   `{v, server, key, enc, timestamp}`. Blink decrypts it with the account's
   private key, and acts only if it is less than 60 seconds old (10 s clock
   skew allowed). A journal replay is too old, and Blink's own export, relayed
   back to it, is ignored.
3. The Data manager comes forward on Import from device, already
   connected. There is no manual connect, by design.
4. Choose **Messages** (category All, Text, Links or Location) or **Files**
   (All, Images, Audio, Video or Other), a contact and a year, month and day.
   Contacts and periods list only what still has something to import. The
   headline says how many new items of how many offered will be added.
5. **Import N** copies them. **Cancel** stops between items. **Disconnect**
   ends the session.

Import is **add-only**. A message whose id is already here is never touched,
removed messages included, so an import does not bring them back. A file
transfer whose message is here but whose file is not gets just the file.

Rows are stored the way journal entries are
(`MessageManager.apply_imported_messages`, in the sync thread), so they get
the same handlers, duplicate check, categories and conversation updates.
Incoming messages arrive as read. When the import ends, unread counts, the
contact list and background PGP decryption follow, as after a journal run
(`BlinkJournalDidApply`).

Files are saved in `file_transfers/<account>/<peer>/<transfer id>/<name>`,
where the conversations look. An encrypted file (`.asc`) is decrypted with the
account's private key and saved without `.asc`.

While connected, the other device is pinged every 4 seconds unless a request
is already in flight. It is declared gone after 3 missed pings and 30 seconds
of silence. What was imported is kept.

Contacts are not imported: they sync through the server's addressbook.

## Export to device

1. Choose the account and press **Start Export**. The account needs PGP
   turned on and its key on this computer: the announcement is encrypted with
   it.
2. Blink reads the account's history once, starts an HTTP server on the
   local network (IPv4, random port 49152–65535) and announces it to the
   account's other devices (`MessageManager.send_data_export_announcement`,
   not journalled).
3. Those devices open their Import screen. **Announce Again** repeats the
   announcement for a device that was off or asleep. The tab shows the
   address, the key, and which device is importing and how far it has got.
4. **Stop Export**, or closing the Data manager window, stops the server.

**What is exported.** A snapshot taken at Start: the account's live text,
location and file transfer rows, shaped as rows of Sylk Mobile's `messages`
table (`msg_id`, `timestamp`, `unix_timestamp`, `content`, `content_type`,
`metadata`, `from_uri`, `to_uri`, `direction`, `state`, `sent`, `received`,
`related_msg_id`, `related_action`, `category`, `has_link`, `encrypted`). A
body that is still PGP ciphertext goes as it is (`encrypted` 1), and the
importing device decrypts it.

**Files** are offered with the bytes Blink has: the decrypted file in its
transfer folder, found under the envelope's transfer id, the URL's id or the
message id, in `file_transfers/` or the older `downloads/`. The row therefore
names the file without `.asc` and carries no `.asc` URL, or Sylk Mobile would
take the plaintext for ciphertext. A transfer whose file is not here is still
offered: its row imports, and `/api/blob` answers 404.

**Contacts** are not exported: `/api/contacts.json` is `[]`. There is no
browser page and no zip download; the server is for the importing app only.

**Security.**
* A one-time 8-character token, accepted as `?token=` or `Authorization:
  Bearer`. Only `/` and `/api/ping` answer without it.
* A fresh 32-byte NaCl secretbox key per export, sent only inside the PGP
  announcement. A client sending `X-Sylk-Enc: 1` gets every body back as
  `nonce(24) || box` with `X-Sylk-Enc: 1`.
* The server lives only while the Data manager window is open.

**Endpoints:** `/api/ping`, `/api/summary`, `/api/calendar`, `/api/ids`,
`/api/rows-bulk`, `/api/idindex`, `/api/contacts-counts`, `/api/meta`,
`/api/blob`, `/api/contacts.json`, with the same parameters as Sylk Mobile:
`kind` is `messages` or `files`; `period` is `all`, `YYYY`, `YYYY-MM` or
`YYYY-MM-DD`, using the local day.

## Code

| File | Role |
|---|---|
| `blink/data_import.py` | the protocol: announcement, secretbox seal/open, the import client, row mapping (no Qt) |
| `blink/data_export.py` | the export snapshot, the transfer storage index and the HTTP server (no Qt) |
| `blink/data_devices.py` | the Import from device and Export to device tabs |
| `blink/datawindow.py` | the tabs in the Data window; stops the export when it closes |
| `blink/messages.py` | `handle_data_export_announcement`, `send_data_export_announcement`, `apply_imported_messages`, `imported_messages_done` |
| `blink/history.py` | `MessageHistory.data_export_rows`, `data_import_inventory` |
| `blink/mainwindow.py` | opens the Data window on Import from device for a fresh announcement |
| `debian/control` | `python3-nacl` for the secretbox |

## Verified

Outside the application:
* `secretbox_seal` with PyNaCl and with the pycryptodome fallback produce
  identical boxes, and `secretbox_open` reverses them.
* Blink's export against Sylk Mobile's real `app/ImportClient.js` and
  `app/ExportCrypto.js` (tweetnacl), under Node: summary, contact list,
  message import (categories and `has_link`), and file import, with a file
  found by its transfer id, one in the older `downloads/` layout, and one
  only partly downloaded and not offered.
* Blink's import client against Blink's export: ids per category, files,
  404, and incoming rows stored as read (`displayed`).

Not yet run in the application: the tabs, the announcement over SIP, and a
real phone.

## Testing

1. Phone to Blink: start the export on the phone. Within a few seconds the
   Data manager opens on Import from device, showing the phone's user agent.
   Import a month of text, then of images. They appear in the conversations,
   as read; the pictures show without being fetched from the server.
2. Import the same selection again: "All N already imported".
3. Turn the phone's Wi-Fi off during an import: after about 30 seconds the
   tab says the connection was lost, and what was imported is kept.
4. Blink to phone: Export to device > Start Export. The phone's Import screen
   opens; import messages and files, and check the files open on the phone.
5. Close the Data manager window during an export: the phone reports the
   connection lost, and the activity log has `[export] Export of ... stopped`.
6. Blink to Blink: start the export on one computer; the other opens Import
   from device by itself.
7. An account without PGP: Start Export is disabled, with the reason shown.
