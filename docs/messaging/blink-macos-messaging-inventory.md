# Blink for macOS — Messaging Feature Inventory

Reference for porting to Blink Qt (Linux) so both clients behave the same.

| | |
|---|---|
| Source tree | `~/work/blink-cocoa` (PyObjC) as of 2026-10-06 |
| Period covered | darcs patches since TAG 9.4.4-classic (2026-06-30) up to "Render PDF files inline" (2026-10-01), versions 9.5.0 – 9.8.5 |
| Companion docs | `blink-cocoa/docs/MESSAGE-PANEL-MIGRATION.md` (original plan — partly superseded, see §1.1) |
| Qt baseline | `~/work/blink-qt` as of 2026-10-06 (column "Qt today" in the tables) |

How to read this document:

- Every feature lists **behaviour**, the **rules/constants** that define it, the **wire format / storage** it depends on, the **macOS code** that implements it, and the **darcs patch(es)** it came from (matched by patch name and date).
- Constants are quoted from the code; they are the spec the Qt port should match.
- Section 20 lists defects found in the macOS code while doing this inventory, so they are not ported.
- Section 21 is a patch timeline.

---

## 0. Summary

| # | Feature area | macOS | Qt today |
|---|---|---|---|
| 1 | Native message pane next to the contact list; contact list is the conversation switcher | done | Separate chat window, session list + `QWebEngineView` per session |
| 2 | Bubble kinds: text, image, video, audio/voice note, PDF, location, file, call record (CDR), system | done | text, image, file, call event; audio only for `sylk-audio-recording*` |
| 3 | Inline media with auto-fetch limits, captions, info panel | done | images only, no captions, no info panel |
| 4 | Message categories + filter bar, paged from SQL; picture grid with month dividers, multi-select | done | absent |
| 5 | Scroll-back paging (50 per page), loaded-range label, search, jump to date | done | last 100 rows, no paging, no search |
| 6 | Reply, edit, copy, forward, delete-for-both, delete removes file | done | delete / delete-for-both only |
| 7 | Unread persisted in DB, read markers synced across devices, own echo swallowed | done | partial — remote read not persisted |
| 8 | SylkServer journal: paged, cached to disk, 5-year first sync, never decrypts | done | single GET, no paging, no first-sync window |
| 9 | HTTP file transfer (upload/download, PGP `.asc`), MSRP fallback, file cache | done | download over HTTP; upload over MSRP only |
| 10 | Bonjour messaging keyed by neighbour instance id, never in XCAP | done | present, keyed `<instance_id>@local` |
| 11 | Location sharing (sylk-mobile compatible) with OSM map bubbles | done | absent |
| 12 | Contact row: last message on 2nd line, unread badge, typing / sharing location, last-activity time | done | unread badge only |
| 13 | XCAP contact normalisation & healing, PSTN E.164, duplicate merge, device stamps | done | absent |
| 14 | Groups: Messages, Calls, Tel, Deleted (two-stage delete), Bonjour | done | Messages (virtual), Bonjour |
| 15 | Addressbook change notifications between devices | done | absent |
| 16 | PGP key escrow in own XCAP contact; Sylk data import | done | absent (manual export/import of keys only) |
| 17 | Call Detail Records as message bubbles + CDR panel | done | call events from local history only |
| 18 | Voice recording, attachment preview (crop, trim, compress), central playback stop | done | absent |
| 19 | Contact mangler (screenshot mode), per-chat language, account picker | done | account selector only |

---

## 1. Message pane

### 1.1 Hosting

The plan put the pane in the audio `NSDrawer`. What shipped is a **split view in the main window**: contact list left, conversation right (`MessagePaneSplitView` in `MessagePaneController.py`, installed by `ContactWindowController.messagePaneHost`). Reason in `MessageHost.py:102-107`: a drawer cannot be wider than its window. `MessageHost.USE_MESSAGE_PANEL = True`; the native renderer is always on (`nib_name = "MessageView"`).

Rules:

- `LIST_MIN_WIDTH = 274`, `PANE_MIN_WIDTH = 320`, default pane width `480`. The list keeps its width; the transcript absorbs resizing.
- Opening the pane grows the window by the pane width (clamped to the visible screen minus an open audio drawer); closing shrinks it back.
- Widths persisted in defaults `MessagesPaneWidth` (only if ≥ 320) and `ContactListWidth`.
- **The pane is never reopened at launch**, because showing it starts marking messages read.
- ⌘4 toggles the pane (`showSMSWindow_` → `showMessagesPane` / `hideMessagesPane`).

### 1.2 Conversation switching

- Selecting a contact **switches** the pane but never **opens** it (`switchMessagePaneToSelectedContact`). The conversation object (viewer) is created on click; nothing arriving on the wire creates one.
- No selection or no messageable target → empty state "Select a contact to see messages".
- Clicking a call follows to that party's conversation only if the pane is visible.
- `openMessagesForURI` (notification click, share extension) shows the pane, selects the row, can prefill the composer and queue attachments. Bonjour neighbours not listed yet are retried 10× at 1.5 s.
- History of a conversation is replayed only when it is first shown (`replayHistoryIfNeeded`). Each conversation keeps its scroll position and unsent text.
- On show: request the peer's PGP public key if missing (once per address per launch).

Code: `MessagePaneController.addViewer / selectViewer / removeViewer_`, `SMSWindowManager.viewerForTarget / presentViewer` (the old `getWindow` split in two), `viewer_hosts` map, heartbeat moved from window to manager.
Patches: Refactor Messaging, Refactor Chat and History to use native widgets instead webview (08-26), Improve chat rendering performance (08-29).

### 1.3 Header (44 pt)

| Element | Behaviour |
|---|---|
| Avatar (28 pt) | Photo or initials on colour. Click → edit contact / address-book card / "Add contact" prefilled for unknown addresses. |
| Name | Address book name → display name → URI. Bonjour: neighbour name without "(computer)". |
| Info line | Remote URI, or "is typing..." while the peer composes. |
| **URI switcher** | If the contact has more than one usable address, a `▾` chevron; click opens "Send messages to:" listing each address, "(XMPP)" etc. for non-SIP types, "(default)" on the default; current ticked. Choosing one switches to the conversation keyed by that address. |
| **Account pill** "From ‹account›" | Shown only when >1 enabled non-Bonjour account and conversation is not Bonjour. Menu "Send messages from:". Follows `BlinkConversationAccountChanged` when an incoming message moves the conversation to another account. |
| Encryption lock | Green: OTR verified or PGP active. Red: OTR unverified. Grey: off. Menu: OTR items, "PGP key ID …" (opens key panel with Copy), Lookup PGP public key, Send my PGP public key. |
| A− / A+ | Transcript + composer font, step 1 pt, 9–28 pt, default system message font (13). Persisted `SIPMessageTranscriptFontSize`. |
| 📅 History | Menu: Jump to Latest; per year → per month "Month (N days)" → per day. Jump loads the page ending at that day. |
| 📍 Location | Only when SylkServer detected (`sms.history_url` set). "Send Current Location", "Request Location" (§11). |
| 📞 / 🎥 | Audio / video call from the conversation's account. |
| ■ Playback stop | Visible while any clip plays anywhere in the app (§18.3). |

**Which URI a contact's conversation opens on** (`SMSWindowManager.messageURIForContact`): the URI that last carried a message (from history), not the XCAP default; an explicit header pick wins until a newer message arrives on any address; the default URI only when no conversation exists. Candidate list `contactMessageURIs`: addressbook order, no blanks, dedup by canonical form, no `://`, no types `url`/`bonjour`.

Patches: Select sip uri in message pane if contact has more than one (09-17), Added Call button to SMS pane (08-28), Improve messaging and contact avatars (08-29), Use system font size for message input bar (09-20), Make PGP key clickable… (09-17).

### 1.4 Composer

- Buttons: 🎤 record, 🙂 smileys, 📎 attach. 📎 and 🎤 hidden when files cannot be sent (no upload URL and no MSRP file transfer).
- 📎 menu: Take Photo… · Grab a Window… · Grab an Area… (`screencapture -W|-s`, one grab at a time) · Choose Files… · Paste from Clipboard. All go through the attachment preview (§18.2).
- Paste a photo directly into the input bar.
- Drag & drop files anywhere on the conversation; refused when the drag started in the same conversation.
- Edit and reply modes show a hint line above the input ("✎ Editing message — press Escape to cancel", "↩ Replying to X: ‹80 chars› — press Escape to cancel").
- **Account confirmation** (`MessageAccountPicker.pick_account`): first message to a new address on an unmatched domain asks "Which account should this conversation use?". Skipped when only one account exists or exactly one account matches the recipient's domain. Preselects the account with most conversations.

Patches: Added attachment preview, Added Grab option to file transfer, Fix take camera snapshot (08-28), Added paste photo in inbut bar (08-30).

---

## 2. Bubbles and inline rendering

### 2.1 Common

- One bubble per message id; a duplicate only updates state.
- **Grouping by turn** (direction, not sender string): avatar and name only at the start of a run; avatar column (32 pt) stays reserved.
- **Day dividers**: "Today", "Yesterday", weekday (< 7 days), "%A, %d %B" (this year), "%d %B %Y". Pruned on delete; hidden when nothing visible under them (filters).
- **Delivery glyphs** (outgoing): ✔✔ displayed (green), ✔ delivered, 🕑 deferred / failed-local.
- **Fills**: private green, sending grey, failed pink; light mode incoming white / outgoing (214,234,245); dark mode incoming blue (59,110,165) / outgoing white. Linen texture background. Links survive dark mode.
- **HTML** is sanitised: allow a, b, i, lists, headings, tables, pre/code; drop script, style, iframe, img, svg, audio/video, form with contents. Link schemes: http, https, sip, sips, tel, mailto, xmpp. Plain text bare URLs linkified. Smileys optional.
- **Allow-list**: only known content types become bubbles (`is_renderable_content_type`); unknown stored rows are never drawn as raw JSON.
- An edited message is re-inserted at its original timestamp.

### 2.2 Kinds

| Kind | Rendering |
|---|---|
| Text | Sanitised HTML or linkified plain text. |
| System note | Centred, green or red. Not a message: no category, hidden when filtering. |
| Image | Inline, max height 320 (640 if source ≥ 640 px), width ≥ 120, Retina decode. |
| Video | Poster generated off the GUI thread once local; duration from container; play badge; plays inline with transport row; "open" hands off to system player. No poster → 9:16 well. Unplayable → file icon (verdict cached per size+mtime). |
| Audio / voice note / call recording | Player once the file is local. Waveform from "peaks" metadata → envelope peaks → measured. 48 bars; call recordings show Remote above Local. Optional spectrogram (16 bands, 10 fps). Seek by click/drag. Machine-named files titled "Call recording", "Video call recording", "Conference recording", "🎤 Audio recording". |
| PDF | Page 1 rendered inline, max height 360; pill "PDF · N pages · size"; no automatic caption; click opens external viewer. |
| Location | Map bubble (§11). |
| File | System file icon (44 pt), "📎 name", "type · size · duration", red "⚠ error" line. |
| Call record (CDR) | ↗/↙ arrow, label ("Missed call", "Answered on another device", …), "duration — reason" (e.g. "Busy Here (486)"), "video call"; red for missed / voicemail / rejected / failed, green when it has a duration (§17). |

### 2.3 Auto-fetch and download

Only bubbles in the viewport, coalesced by a 0.3 s timer (`FileTransferCache.py`):

| Type | Auto-download limit |
|---|---|
| Image | `MAX_AUTO_IMAGE_BYTES = 8 MiB` |
| PDF | `MAX_AUTO_PDF_BYTES = 10 MiB` |
| Video | `MAX_AUTO_VIDEO_BYTES = 20 MiB` **and** ≤ `AUTO_VIDEO_MAX_AGE_DAYS = 7` |
| Audio, other | never |

Also skipped: encrypted with no key loaded; failure already recorded. Click on the bubble or "Download" forces a fetch; progress bar polled every 0.2 s. Outgoing uploads render the local original immediately.

Failure handling: transient (5xx/network, forgotten on quit) vs permanent (4xx, undecryptable with key present; written into the stored envelope `error` field, cleared on success) vs **gone** (404/410): the message is hidden and deleted locally (not announced) unless the file is already on disk.

Patches: Fix download detection (08-28), Fix loading image cache (08-30), File transfer fixes (08-29, 08-31), Fix retry data transfer (09-29), Render PDF files inline, No pdf caption (10-01), Added video sending preview (08-30).

### 2.4 Captions

- Shown centred under a picture or movie. User caption wins; otherwise recording title. None on grid tiles. None by default on PDFs.
- Wire: separate `application/sylk-message-metadata` message `{action:"label", messageId:<transfer id>, value:<caption>}`; newest wins; empty value clears. Compact JSON (`separators=(',',':')`) — removal matching relies on it.
- **Edit Caption only for outgoing** image/video with a transfer id and no failure: header ✎ or menu "Edit Caption…"; multi-line dialog.
- Caption can be set in the attachment preview when sending a single picture/movie; the label is sent after the transfer id exists.

Patches: Added caption to images (09-17), No pdf caption (10-01).

### 2.5 Per-message actions (bubble header)

Order: ✎ edit/caption · ⧉ copy · ↗ open / ↧ save-as · ↩ reply · ⓘ info · sender · lock · time · ticks · ✖ delete.

| Action | Rules |
|---|---|
| Delete ✖ | Confirm "Delete this message?" / "Delete “file”?". Checkbox "Delete it for ‹peer› too" only for outgoing and not in a conversation with yourself. Remote delete sends `application/sylk-api-message-remove` (bare msgid); Bonjour sends `application/sylk-message-remove` JSON peer-to-peer. **Deleting a file-transfer message deletes the downloaded file** (`FileTransferCache.purge_transfer`). Stops playback of that message. |
| Edit ✎ | Outgoing text only (not transfers). Loads text into composer; resend = delete + send with the original timestamp. |
| Copy ⧉ | Text as text; picture as image; PDF as file URL; location as "lat, lon". Green ✓ for 1.4 s. |
| Open / Save-as | Open when local; Save-as fetches then save panel. |
| Reply ↩ | Both directions; not on tiles, system notes, dates, calls. Sends a `reply` metadata link **before** the reply. Reply bubble shows a quote block (sender, ≤ 3 lines, ≤ 240 chars digest, 40 pt thumbnail for image/video/PDF). Clicking the quote scrolls to and flashes the original (1.6 s); if off-page: "Loading the original…", or "Original message not available". |
| Info ⓘ | Message Info panel (§2.6). On a CDR opens the call details panel. |
| Forward | From grid multi-select (§4.3). |

Text inside a bubble is selectable; copying a range across bubbles is **not** implemented.

### 2.6 Message Info panel

Sections: **Message** (ID, direction, from, time, sender timestamp, content type, category, size, encryption, private, caption) · **Delivery** (outgoing: stored status vs bubble vs session, mismatches shown; in send queue; PJSIP id; read; deleted) · **Replies** · **File transfer** (all envelope keys, local file or "Not downloaded", failure, encrypted) · **Location** (lat/lon, track points) · **Storage** (local/remote URI, CPIM From/To, Call-ID, journal id, related to/action, metadata JSON) · **Related messages**. Modal, values selectable, links clickable, Escape closes.

Patches: Added message info panel / Fixed saving metadata replies (09-18), Added info button to image bubbles (09-29).

---

## 3. Transcript: paging, range label, search

- **Page size `showHistoryEntries = 50`** renderable messages. `renderable_cutoff()` finds the timestamp of the 50th-newest renderable row (location trail ticks and metadata sidecars excluded); page fetched by time with a safety cap 500 rows (150 without cutoff). "More available" by probing one row older than the oldest shown.
- Older pages are **prepended with the viewport anchored** (no jump).
- Strip above the transcript: `loaded range — history note — hint`:
  - Range: "N messages, 12 Jul 08:00 – 26 Aug 17:40" (one date if same day), counting visible non-divider, non-system bubbles.
  - Hint "Hold up-scrolling to load more messages..." **only** when more history exists, at least one message is shown, **and the user has scrolled up themselves** (scroll-wheel or drag events only).
  - Notes: "Loading messages...", "Loading previous messages...", "Messages could not be loaded".
  - Empty rows collapse; the search field and range label stay consistently visible when switching contacts.
- **Search**: field visible while a page is shown, a query exists, or stored history exists. Query is SQL `body like '%text%'`; hits marked red.
- **Jump to date** from the header calendar (§1.3).

Patches: Fixed scrolling label (08-30), Fix loading the views above message pane category bars (09-15), Fix show conversation interval loaded labels (09-16), Only show keep scrolling messages if users already scrolled up (09-16).

---

## 4. Message categories, filter bar, grid

### 4.1 Classification

`HistoryManager.classify_category(content_type, body, related_action, metadata)` — mirrors sylk-mobile `app.js _classifyMessageCategory` and runs **at insert, without decrypting**. Stored in column `category`.

1. empty content type → NULL
2. `text/pgp-public-key`, `text/pgp-private-key` → NULL
3. `application/blink-call-detail-record` → `call` (Blink only)
4. `text/*` → `text`
5. `application/sylk-file-transfer`, `application/vnd.gsma.rcs-ft-http+xml` → by **extension first** (trailing `.asc` stripped): jpg jpeg png gif heic webp tiff bmp → `image`; mp4 mov m4v avi mkv webm → `video`; mp3 m4a wav aac ogg opus caf → `audio`; `call_recording:true` → `video` if video ext else `audio`; else mime prefix image/audio/video; else `other`. Armoured / unparseable envelope → NULL, stamped later when decrypted.
6. `application/sylk-location-sharing` → `location` only for coordinate **origins** (and one-shots); NULL for trail ticks and signals.
7. anything else → NULL

PDFs fall into `other`. `links` is not stored: it is `text` narrowed with a URL regex.

### 4.2 Filter bar

- Chips: **All** + present categories among `text, links, audio, image, video, location, call, other`; shown only when ≥ 2 categories are present.
- **Present categories come from the whole stored history**, not the loaded page: `select distinct category … where category is not null` plus a LIMIT 1 links probe (`body like '%http://%' / '%https://%' / '%www.%'`).
- **Filtering pages from SQL** like the unfiltered view: choosing a chip empties the transcript and loads the newest 50 of that category; scrolling up loads 50 more. Sidecar rows (waveforms, reply links) for the page are added back (`related_messages`, chunks of 500).

### 4.3 Grid mode

- For `image`, `video`, `location`. Columns 2–6, default 3 (defaults key `MessageGridColumns`). Photo tiles centre-cropped 4:3 (anchor 0.28), 2 pt spacing; location cells stay full bubbles (8 pt).
- **Month dividers** `%B %Y` split the grid into one grid per month (day dividers remain for messages).
- Tile (i) info button, size pill; video grid has "Download all" for the viewport.
- **Multi-select**: checkbox per tile, shift-click extends (add only). Floating bar "N selected · Forward… · Delete · Done". Delete: one confirmation; remote delete offered only for the outgoing ones ("Delete the N you sent for X too"). Forward: menu of the 12 most recent other conversations. Drag a ticked tile drags all ticked local files.
- Tile context menu: Info, Copy, Open, Show in Finder, Save As…, Edit Caption…, Delete…, Delete N Ticked….

Patches: Improve message history and category filtering (08-30), Add multiple items selection in grid mode (08-31).

---

## 5. Unread, read markers, typing

### 5.1 Visibility rule (read receipts boundary)

A conversation is **visible** = selected **and** pane visible **and** main window key (`isConversationVisible`). Becoming visible clears unread and starts sending IMDN `displayed` for queued incoming messages; losing visibility pauses the queue. Selecting a conversation always clears its badge.

### 5.2 Persistence

- Column `read` (`INTEGER DEFAULT 1`, schema v9; existing history migrates as read). Only incoming rows are ever 0.
- Live intake stores `read=0` when the message counts as unread, is incoming, not replicated, not on screen and not already displayed.
- At launch: `unread_counts()` = `select remote_uri, count(*) … where read=0 and direction='incoming' and not deleted group by remote_uri` → in-memory counts keyed by canonical URI (Bonjour: instance id).
- Counts announced by `BlinkUnreadMessageCountChanged(key, count, total)` → contact rows, Dock badge ("calls / chats").

### 5.3 Cross-device read markers

- On a real unread→read transition send `application/sylk-api-conversation-read` `{"contact": remote_uri, "device_id": this_device_id()}`. Requires `sms.enable_replication`; never for Bonjour.
- Incoming `application/sylk-conversation-read` (`{"contact"}` or bare URI): clear unread for that contact, mark open viewers' messages displayed, **persist** via `mark_conversation_read`.
- **Own echo swallowed silently** — not applied, not logged: if payload has `device_id`, echo = it equals this device; otherwise any own send of a read marker within `OWN_CONVERSATION_READ_TTL = 30` s consumes one echo.

### 5.4 Typing

- Header info line "is typing..." and contact row "✎ is typing…". State per canonical key, expires after `refresh` (default 120 s) + 1.
- **Own is-composing echoes (replicated outgoing) ignored.**
- Outgoing active/idle sent with `Refresh 60`.

Patches: Filter out conversation read echos (09-11), Skip my own is-composing messages (09-17).

---

## 6. SylkServer journal

### 6.1 Token and triggers

- `application/sylk-api-token` request (`'I need a token'`, no CPIM) to own account; reply JSON `{token,url}` → `sms.history_token`, `sms.history_url`. Rate-limited `TOKEN_REQUEST_INTERVAL = 30` s per account; not gated on replication (uploads need the token).
- Sync on `SIPAccountRegistrationDidSucceed` (+10 s), token change, replication enabled. 401 → re-request token after 30 s.
- `tls_name` passed into proxy lookup (token requests over TLS were not delivered without it); `sylk.link` accounts use `tls_name='sip2sip.info'`.

### 6.2 Download (stage 1)

- URL `history_url` (`@` → `%40`) + `/<history_last_id>`; **first sync** (no cursor): `?since=<now − 5 years>` (`JOURNAL_SINCE_YEARS = 5`, ISO ms + `Z`). Needs a SylkServer that honours `since`; unpatched servers return now − 3 days.
- `Authorization: Apikey <token>`, gzip, 20 s timeout.
- Each page written to `ApplicationData/journal/<account>/<timestamp>-<last id>.json` as `{"cursor", "messages"}`; cursor `sms.history_last_id` advances **only after the file is written**. Cap `MAX_JOURNAL_PAGES = 200` per run.
- Per-account in-progress guard, released in `finally`.

### 6.3 Apply (stage 2)

Cached files applied in chronological order in **bulk mode** (no viewers created, notifications coalesced, progress logged every 250 entries, 50 ms throttle per chunk).

| Journal content type | Action |
|---|---|
| `application/sylk-conversation-remove` | tombstone conversation, floor = entry timestamp |
| `application/sylk-message-remove` | tombstone the **target** id from the payload |
| `message/imdn` | **skipped on first sync**; otherwise update status |
| `application/sylk-conversation-read` | apply read; also cancel that contact's pending "while away" banner |
| `text/pgp-public-key` | save `keys/<uri>.pubkey` (not own, not outgoing, not Bonjour into XCAP) |
| `text/*`, `application/sylk-location-sharing`, `application/sylk-message-metadata`, file transfers | store; incoming counted for banner only if notable |
| `application/blink-call-detail-record` | merge CDR (§17) |
| `application/sylk-addressbook-update`, `application/sylk-data-export`, `application/sylk-contact-update` | ignored |
| anything else | stored verbatim, `read=1`, inert |

Rules:

- **The journal must not decrypt anything** — bodies stored as ciphertext (`encryption='pgp_encrypted'`); notability decided from the cleartext envelope; decryption at render time (`replay_history → update_decrypted_message`, preview builder, location bubbles).
- **A journalled message never creates a conversation**; with no open viewer it is only persisted (unread if notable and not displayed). With an open viewer it is presented.
- Out-of-order tolerated: reply link before/after reply, location stop before origin, recording note vs transfer, caption with older timestamp.
- End of run: banners "N message(s) received while you were away" for < 4 senders, else "Offline messages received / From N contacts"; Messages group membership ensured; previews refreshed.
- First sync on a device: promote Messages group to top; send once "Account activated on ‹user agent›" to own account (`sms.activation_announced`).

Patches: Log journal actions, Fetch last 5 years at 1st journal sync, Save activation anouncement (08-28), Improve chat rendering performance (08-29).

---

## 7. Content types (live + journal)

| Content type | Direction | Handling |
|---|---|---|
| `text/plain`, `text/html` | both | message; PGP-encrypted when peer key known |
| `message/imdn` | both | delivery/display status; never creates a viewer |
| `application/im-iscomposing+xml` | both | typing; own echo ignored |
| `application/sylk-api-token` | out / in reply | token handshake |
| `application/sylk-api-pgp-key-lookup` | out | key lookup, once per (account, uri) per launch, only when the user sends something |
| `application/sylk-api-message-remove` | out | remote delete (bare msgid) |
| `application/sylk-api-conversation-read` | out | `{"contact","device_id"}` |
| `application/sylk-api-conversation-remove` | out | `{"contact","timestamp"}` to own account(s) |
| `application/sylk-conversation-read` | in | apply / swallow own echo |
| `application/sylk-conversation-remove` | in | tombstone up to CPIM timestamp |
| `application/sylk-message-remove` | in (and out for Bonjour) | `{"message_id","contact"}` |
| `application/sylk-file-transfer` | both | JSON envelope (§9) |
| `application/vnd.gsma.rcs-ft-http+xml` | in | normalised to the same envelope |
| `application/sylk-location-sharing` | both | §11 |
| `application/sylk-message-metadata` | both | sidecars: `reply`, `label` (caption), `peaks` (waveform), `call_recording`, legacy `location`. Stored `read=1`, never a banner, never stamps conversation time |
| `application/blink-call-detail-record` | in (self) | §17 |
| `application/sylk-addressbook-update` | both (self) | §14.3; sent with `X-Sylk-Skip-Journal: yes` |
| `application/sylk-data-export` | in (self) | §16.2 |
| `text/pgp-public-key` | both | key file |
| `text/pgp-private-key` | in (self) | import dialog after public key comparison |

Send rules: no CPIM for the `sylk-api-*` types and `text/pgp-public-key`; renderable messages carry CPIM `Message-ID` + `Disposition-Notification: positive-delivery, display`; OTR messages carry `X-Sylk-Skip-Journal: yes`; CPIM header `agp.Metadata` (ns `urn:ag-projects:xml:ns:cpim`) carries location v2 / CDR metadata. Bonjour adds `instance_id` to the From URI.

---

## 8. Storage (`HistoryManager.ChatHistory`, schema v22)

Columns added to `chat_messages` since July:

| Column | Since | Purpose |
|---|---|---|
| `read` INTEGER DEFAULT 1 (+ index) | v9 | persisted unread |
| `metadata` LONGTEXT | v10 | whitelisted cleartext envelope (location v2, CDR JSON) |
| `related_msg_id`, `related_action` (+ index) | v10 | names match sylk-mobile; tie trail ticks and sidecars to their owner |
| `category` TEXT | v10, backfilled v12/v13, locations fixed v14, calls v22 | filter |
| `expire_time` INTEGER DEFAULT 0 | v10 | reserved (unused) |
| `deleted`, `deleted_time` INTEGER DEFAULT 0 | v15 | tombstones |
| indexes `(remote_uri,time)`, `(remote_uri,category,time)` | v11, v12 | paging |

Other migrations: v7/v8 drop bad metadata rows; v16–v20 fix call rows (direction, failed reason, PSTN spelling, media type, convert HTML call rows to CDR records); v21 deletes presence rows ("X is now offline" is no longer history). Every reader filters `NOT_DELETED_SQL = "(deleted is null or deleted = 0)"`. Unique key `(msgid, local_uri, remote_uri)`; on duplicate only `status`/`journal_id` update.

`add_message` rules worth porting: coerce content type to plain `str`; derive category; drop the echo of our own self-transfer; merge CDRs by `(local_uri, sip_callid)`; parse CPIM timestamp to naive UTC.

---

## 9. File transfer

### 9.1 Envelope

`application/sylk-file-transfer` JSON: `{filename, filetype, filesize, transfer_id, url, until, sender{uri}, receiver{uri}, direction, duration?, encrypted?, call_recording?, error?}`. RCS XML normalised to the same dict.

### 9.2 Upload over HTTP (the POST is the send)

- Base URL: `sms.file_transfer_url` (learned from the first incoming transfer — last 4 path segments stripped, must end in `/filetransfer`) else `history_url.split('/messages')[0] + '/filetransfer'`.
- URL `<base>/<sender>/<receiver>/<transfer_id>/<filename>` (whitespace and `:` → `_`).
- Order: copy into cache → show bubble from local copy → upload.
- PGP if enabled, keys present and `filesize ≤ MAX_ENCRYPT_BYTES = 50 MB` (decimal): AES256, to peer and self; `.asc` suffix, `encrypted=true`.
- `Authorization: Apikey <history_token>`; 401 → request token.

### 9.3 MSRP fallback

No base URL (Bonjour, plain proxy) → same envelope without `url`, sent over MSRP with an SDP-shared `transfer_id`. Offline Bonjour neighbour → `FAILED_LOCAL`, retried when it reappears (within 7 days). Incoming MSRP files are filed as synthesized envelopes so they render like HTTP ones.

### 9.4 Cache

`ApplicationData/file_transfers/<account>/<peer>/<transfer_id>/<name>`; download to `.part-xxxx` then rename; decrypt with the key matching the PGP key ids. Temp folders (`.tmp_screenshots`, `.tmp_snapshots`, `.tmp_file_transfers`) swept at launch. Purge per transfer (message delete), per peer (conversation purge), move per peer (Bonjour re-key).

---

## 10. Bonjour messaging

- **Identity of a long-term chat = the neighbour's instance id** (From URI parameter `instance_id`, stored bare without `urn:uuid:`). History key: `remote_uri = <instance_id>`, `local_uri = 'bonjour@local'`. Old `urn:uuid:X` rows folded into `X` (history and files moved).
- Offline neighbours keep a row in the Bonjour group (rebuilt from history `bonjour_conversations()`), placeholder address `sip:<id>@bonjour.local`, kept out of routing/filing.
- **Never creates or writes XCAP contacts**: public keys from neighbours not filed, not added to Messages group, not purged by contact deletes. Renames stored locally in `bonjour_neighbours.json`.
- Routing to the neighbour's current link-local URI; pending messages and transfers resent when it reappears; retry storm stopped while offline.
- Remote delete sent peer-to-peer (`application/sylk-message-remove`); no conversation-read announcements.
- Files over MSRP (no server). Notifications use the neighbour's nickname/announced name.
- Bonjour group position: first if the Bonjour account is the default, else last.

Patches: Implemented Bonjour messaging, Reposition Bonjour group when activating account (08-31), Fox posting notifications for Bonjour neighbors (09-10).

---

## 11. Location sharing

Wire format is sylk-mobile's (`SylkLocation.py` mirrors mobile `app/locationEnvelope.js` and the server's `webrtcgateway/location.py`).

- Content type `application/sylk-location-sharing`; legacy `application/sylk-message-metadata` with `action:"location"` is read, never sent.
- Versions: **v2** — body is only the PGP-armoured coordinates (or empty for signals), cleartext envelope in CPIM `agp.Metadata` / journal metadata column; **v1** — JSON envelope with ciphertext in `value`; **legacy** — whole envelope armoured.
- Actions: coordinates `location_once, location_start, location_update, meeting_request, meeting_start, meeting_update`; signals `location_request, meeting_accept, meeting_reject, location_stop, meeting_end`. Notable (badge + conversation time): `location_once, location_start, meeting_request`. Updates move the pin, never open a bubble.
- Session key: `sessionId` then `messageId`. Meetings: one bubble per `<session>:<role>`; destination drawn as a green pin.
- **Blink sends only**: `location_once` (current location, one-shot, v1 body, cleartext coordinates by design) and `location_request` (`expires` 24 h). It renders live shares and meetings from mobile but does not originate them.
- Live trail: up to `MAX_TRACK_POINTS = 1000`, ordered by timestamp, identical points collapsed; stored merged (`merge_location_bodies` keeps the longer trail) so live, replication and journal writers don't flatten it.
- System notes: "📍 X started sharing live location", "…stopped sharing", "…location sharing expired", "asked for your location", "Meet-up request by X", "🎉 You met", …; footer labels Returned / Sharing expired / Track ended / You met / Meet-up expired / ended / declined.
- **Map bubble**: OSM tiles `https://{a,b,c}.tile.openstreetmap.de/{z}/{x}/{y}.png` with a proper User-Agent; disk cache `ApplicationData/map_tiles/z/x/y.png`; default zoom 15 (3–19), zoom/pan/recentre buttons, map height 140–420; red pin, green destination pin; track as casing + blue line with arrows; **slider shows the track up to the selected point** (live end followed unless scrubbed back); caption "📍 lat, lon (±N m) — click to open in Maps"; click opens Apple Maps; drag exports a PNG snapshot.
- Contact row shows "📍 is sharing location…": one-shot lease 5 s, live lease `min(300 s, expires)`, cleared by stop/end/reject. In memory only.
- Sending current location: CoreLocation one-shot, 20 s timeout, 10 m accuracy; menu item disabled with the reason when unavailable. Needs `NSLocationWhenInUseUsageDescription`.

Patches: Added Location Framework (08-26), Only show track up to selected point by slider (08-28), Show location sharing state in contact (08-30), Remove Location entitlement for Pro target (08-30).

---

## 12. Contact list row

| Element | Rule |
|---|---|
| **2nd line** precedence | `✎ is typing…` → `📍 is sharing location…` → **last message preview** → contact detail |
| Last message preview | Messages group rows only. Newest **text** message (category `text`, not deleted), decrypted if needed (plaintext written back), across all spellings of the contact's URIs (PSTN variants, Bonjour id). Skips keys, "call ended", "Public key received", synthetic location/meeting texts, pure-emoji reactions ≤ 24 chars. Whitespace collapsed, truncated at `CONVERSATION_PREVIEW_CHARS = 100` with `…` (same as mobile `buildLastMessage`). Files, locations and CDRs leave the previous text. |
| Time on the right | `HH:MM` today, "Yesterday", weekday < 7 days, `%d %b` this year, else `%d/%m/%y`. Messages group: last message; Calls/Tel: last call. Name/detail truncate before it. Moon icon when the remote's local time is night. |
| Unread badge | Red pill on the avatar's top-right, "99+" cap, summed over all URIs (or Bonjour id). |
| Avatar | Circle 28 pt; photo or two initials (last two digits for numbers) on one of 8 colours chosen by `crc32(name.lower())`. |
| Ordering | Messages group by last message time, Calls/Tel by last call time (stable alphabetical then time desc); selection preserved on re-sort. |

Updates: per-key row reloads (`BlinkConversationPreviewChanged`, `BlinkUnreadMessageCountChanged`, `BlinkComposingStateChanged`, `BlinkLocationSharingStateChanged`), coalesced during journal bulk; never a full `reloadData`.

Patches: Show last message on Contact tile 2nd line (09-17), Improve messaging and contact avatars (08-29), Show location sharing state in contact (08-30).

---

## 13. XCAP contacts: normalisation and healing

### 13.1 Shared attributes with sylk-mobile

- `SharedSetting.set_namespace('ag-projects:sipsimple')` (was `ag-projects:blink`) — the attribute bag mobile reads/writes; key escrow lives there too.
- Contact shared: `organization, auto_answer, preferred_media, disable_smileys, modified_by, modified_agent, modified_at, modified_reason, modified_hash`. Local only: `disable_chat_history, chat_language, silence_notifications, no_mediaproxy, public_key, public_key_checksum, icon_info`.
- Group shared: `kind` (`calls, tel, blocked, conference, favorites`; empty = user group) + `modified_*`. URI shared: `position`.
- Group identity resolved kind → name → reserved id (`_calls, _tel, _blocked, _conference, _favorites`; `_messages`, `_deleted` by id). `stamp_group_kinds` only fills a missing `kind`, never overwrites.

Patch: Fix sharing xcap attributes with mobile version (08-28).

### 13.2 Canonical URI

`SMSWindowManager._canonical_uri`: strip `sip:`/`sips:`, cut at `;` and `?`, lowercase, then `canonical_pstn_uri` → bare `+E164` for phone numbers (domain dropped), else `user@host[:port]`. Key for conversations, unread, previews, Messages-group dedup.

### 13.3 PSTN normalisation (`util.py`)

`pstn_e164(number, account)` → `+E164` or None, using account `pstn.idd_prefix`, `pstn.prefix`, `pstn.replace_leading_zero` (new setting, e.g. `0031`, = mobile `pstn.replaceLeadingZero`):

1. Strip one trunk zero after the home country code (`+CC0…`, `00CC0…`), digits only.
2. Single leading `0` (not `00`/IDD) replaced by `replace_leading_zero`; Italy/Vatican/San Marino (`39`, `378`) keep the 0.
3. `+` → digits; else strip IDD then `00`; else None.
4. All digits, length ≥ 8.
5. Retry with the external-line `prefix` stripped.

`canonical_pstn_uri`: `@guest.` / `@anonymous.` → `anonymous@anonymous.invalid`; else E.164; else lowercased URI. `same_phone_number`, `pstn_uri_spellings_for_accounts` (every spelling history may hold, used for previews and last times), `BlinkContact.matchesURI` tail match (> 7 digits, handles `00`). New contacts from messaging/calls store numbers bare E.164 with type `tel`.

Patches: Implemented CDR meessage bubble and PSTN contacts normalization, Fix matching PSTN display name with 00 (09-09).

### 13.4 Repair pass (writes back to XCAP, `modified_reason='repair'`)

Once per process, 15 s after `XCAPManagerDidReloadData` (`XCAP_SETTLE_DELAY`), with addressbook notifications suppressed:

1. `user@videoconference.X` → `user@conference.X`.
2. Phone URIs → E.164.
3. Names that just echo the address replaced (room → room number; same number in another spelling; `@` names matched on local part) by an address-book name if found, else the address.
4. Duplicate URIs inside one contact removed; default URI moved to the survivor.
5. `file_contacts_into_kind_groups`: PSTN contacts into Tel, rooms into Conference (add-only).
6. **After** notifications resume: `mergeMessagesGroupDuplicates()` (announced so other devices drop the copies).

### 13.5 Duplicate merge in Messages

Union-find over contacts of `_messages` sharing any canonical URI; **survivor = lowest id** (all clients keep the same copy). Survivor takes the name of the most recently renamed loser if it isn't user-renamed itself, and every URI it lacks; losers deleted; single transaction. Not run on the cached document at launch.

Patch: Merge duplicate Messages contacts after the document is applied, keeping the lowest id (09-30).

### 13.6 Device stamps (`AddressbookOrigin.py`)

Every contact/group save that changes the fingerprint is stamped `modified_by=<instance id>`, `modified_agent=<UA>`, `modified_at` (UTC `%Y-%m-%dT%H:%M:%SZ`), `modified_reason` (`repair, call-history, backfill, group-kind, ensure-group, file-into-kind-group, block`, empty = user), `modified_hash` (sha1 of canonical JSON of name + URIs + policies, or group name + members, first 16 hex). Local-setting-only saves not stamped; remote documents keep the original writer's stamp. On reload each change is logged with "by ‹device› at ‹time› (‹reason›)". Same rules as mobile `addressbookOrigin.js`. Snapshot `ApplicationData/addressbook_origins/<account>.json`.

Patch: Track which devices changed a group or a contact (09-16).

### 13.7 Contact removed on another device

Remote deletion (`data.remote`) keeps history, files and public key. Local deletion purges only addresses no other contact still lists (never Bonjour keys): history rows, file cache of the peer, `keys/<uri>.pubkey`.

Patch: Keep the history, files and key of a contact removed on another device (09-30).

---

## 14. Groups and contact operations

### 14.1 Special groups

| Group | Rules |
|---|---|
| Messages `_messages` | Created on first conversation, **promoted to top** (position 0, expanded) at creation and after first journal sync. Membership audited against history at launch. Not deletable; users cannot add/remove. Bonjour, placeholders, non-addresses never filed. Sorted by last message time. |
| Calls `_calls`, Tel `_tel` | Filled from call history (Tel = E.164 contacts). Sorted by last call. Edit Contact shows them disabled (cannot remove membership). |
| Deleted `_deleted` | Real XCAP group, position 1, collapsed. A contact in Deleted appears only there; other memberships kept for restore. |
| Bonjour | Virtual; first if Bonjour is the default account else last. |

### 14.2 Two-stage delete (sylk-mobile model)

1. **Delete** → "Move '%s' to the Deleted group?": tombstone every conversation key (`deleted=1`), close viewers, drop preview/unread, move contact to Deleted, out of Messages.
2. **Delete Permanently / Empty Deleted Group** → "Permanently delete '%s'?" (warns: removed from server, messages and files deleted on all devices): keys still claimed by another contact restored; contact deleted from XCAP; local purge; `application/sylk-api-conversation-remove` `{"contact","timestamp"}` announced from each replicating account; own echo swallowed (`OWN_CONVERSATION_REMOVE_TTL = 60` s).
- **Restore** puts the conversation back (and into Messages if rows were restored).
- **Remote conversation removal** tombstones only rows older than the removal time and files the contact under Deleted; **a message newer than the removal revives it** (same as mobile `_reviveDeletedContactForActivity`).
- Context menu for a row whose address is tombstoned: "Restore Conversation", "Delete Conversation Permanently...".

Patches: Implement conversation remove and Delete group (08-31), Make Delete multi stage (09-21).

### 14.3 Addressbook change notification (`AddressbookNotify.py`)

`application/sylk-addressbook-update` to own account, `X-Sylk-Skip-Journal: yes`, body `{"v":1,"origin":<instance id>,"timestamp":int,"contactIds":[…],"groupIds":[…]}`; on overflow (`ID_LIST_CAP = 64`) `"truncated":true` and both lists omitted (absent = assume all changed).

- Send: debounce 2 s, max defer 30 s, min interval 10 s, fuse 6 per 300 s; only when XCAP is in sync. **Not sent** for copies applied from the server (`data.remote` from sipsimple), during repairs/reloads, or for Bonjour.
- Receive: only from own account, ignore own origin and stale (> 120 s) ticks; jittered fetch 2–9 s, min 15 s, fuse 10/300 s, backoff 30/60/120/300 s; fetch `resource-lists` with ETag cleared; one retry after 20 s. Replayed journal ticks ignored.
- Spec shared with sylk-mobile `docs/messages/sylk-addressbook-update.md`.

Patches: Send message notification when addressbook changes (09-08), Don't announce addressbook copies tagged remote by sipsimple (09-17).

### 14.4 Multi-selection

- Shift extends **within one group only** (group row selected alone).
- Menu: **Start Conference** (Join Conference panel prefilled with default URIs), **Merge Contacts...** (first selected is the target; URIs and first non-default avatar merged; history kept), **Delete Contacts...** (stage 1). In Deleted: Restore / Delete Permanently. Group menu: Delete Contacts… / (Deleted) Restore All, Empty Deleted Group….

Patches: Allow selection of multiple contacts, Added merge multiple selected contacts, Make Delete multi stage (09-21).

### 14.5 Edit Contact

Public key id clickable (last 16 hex of fingerprint, same as mobile/GnuPG) → key panel; "XCAP" pill shows this contact's `<entry>` or the whole resource-lists document (account, URL, ETag, Copy). Messages/Calls/Tel memberships shown disabled.

Patches: Make PGP key clickable and add show xcap document for contact (09-17), Reuse original PGP key id as label (09-03).

---

## 15. Presence lines

"X is now offline" and similar presence changes are not shown in the message pane and not stored in history (migration v21 deletes old rows).

Patch: Fix duplicate presence lines (08-30).

---

## 16. PGP key escrow and data import

### 16.1 Key escrow

- Private key stored in the `keys` attribute of the user's **own XCAP contact(s)** (ns `urn:ag-projects:sipsimple:xml:ns:addressbook`), symmetrically encrypted with the account password: `{"private_key","public_key","device":"<host> (Blink)","timestamp"}`. Compatible with sylk-mobile.
- On every XCAP reload: restore the key if there is no local one (older key archived as `.privkey.<fingerprint>`); write escrow only if a key exists and nothing is escrowed (once per session). Refuses when the contact is shared with another account or the escrow belongs to another account. Self-check by round-trip decrypt before upload.
- New key generation (RSA 4096) waits until the escrow question is answered (XCAP loaded, disabled, or 15 s).

Patches: Added PGP key escrow, Added Key Escrow plumbing, PGP Escrow fix (08-28).

### 16.2 Sylk data import

- Triggered only by a fresh `application/sylk-data-export` announcement from own account (`{v, server, key, enc, timestamp}`, 60 s freshness), never stored.
- Client of the phone's export API (`/api/ping, summary, calendar, idindex, ids, rows-bulk, meta, blob`), XSalsa20-Poly1305 encrypted responses (`X-Sylk-Enc: 1`).
- **Add-only, messages + files** (not contacts); skips ids already present (including tombstoned); batches of 500 messages / 20 files; PGP blobs decrypted with the matching local key. Heartbeat 4 s; phone lost after 3 failed pings / 30 s silence.

Patches: Added Sylk data import plumbing, Keep cypher used by data import (09-24).

---

## 17. Call Detail Records

- `application/blink-call-detail-record`; record in the metadata column / CPIM metadata, body = plain summary. Fields: `version, sessionId, direction, outcome, duration, remoteParty, source`, optional `displayName, status, reason, startTime, stopTime, timezone, fromTag, toTag, proxyIP, answeredBy, sipTraceUrl, media[], local{deviceId, streams…}`.
- Merge by `(local_uri, sessionId)`; source rank `migrated < local < device < server`; lower rank cannot overwrite authoritative fields.
- Accepted live only from own account; own device's records dropped. Server CDRs polled every 300 s from `settings_url?action=get_history`.
- Bubble per §2.2; "Call Detail Record" panel: Call (party, direction, result, SIP status, times, duration, media, codecs, encryption, recording), Devices (account, answered by, logged by, user agents), SIP (Call-ID, tags, proxy, "Open SIP Trace").
- Category `call`.

Patches: Work on CDR sync (09-08), Implemented CDR meessage bubble… (09-09), Added call detail record modal (09-11), Added codecs and UA to the call detail record (09-21).

---

## 18. Media: recording, preview, playback

### 18.1 Voice recording

Click 🎤 to start/stop (no hold). One recorder app-wide; starting stops any playback. Recorder bar replaces the composer (text preserved): recording dot, level strip, clock, cancel; preview with scrubbable waveform, discard, send. AAC 16 kHz mono 32 kbit/s, max `MAX_RECORDING_SECONDS = 600` (auto-stop). Peaks at 20 Hz sent as a `peaks` metadata message.

Patch: Added Audio recording (08-27).

### 18.2 Attachment preview

Modal before sending. Single picture: downscaled copy shown, crop rectangle (Crop / Revert). Movie: inline player, Mark In / Mark Out, Trim. "Send original" checkbox default from `file_transfer.send_media_as_original` (default off = downscale like mobile). Movies re-encoded behind a cancellable progress window, then "Send this smaller version?" (Send / Send Original (size) / Back / Cancel). Caption field for a single picture/movie. Multiple files listed as rows. Also used for contact photo crop.

Patches: Added attachment preview (08-28), Added video sending preview (08-30), Added Share extention (08-30).

### 18.3 Playback

**Only one clip plays at a time application-wide**; leaving a conversation does not stop it; the header ■ stop button is the control (visible while anything plays). Deleting a message or closing its conversation stops only that conversation's clip. Playing a finished clip restarts it.

Patch: Added centralized playback stop button (08-29).

---

## 19. Other features and settings

| Feature | Notes | Patch |
|---|---|---|
| Contact mangler | `gui.mangle_contacts` (Advanced → GUI "Mangle Contacts"). Runtime-only deterministic mapping to invented names for screenshots; **domain kept, only user part replaced**; numbers keep `+`, first two digits and separators. Display surfaces only (contact list, message pane, menus, history viewer); editors, logs and notification banners keep real values; mangled fields can't be saved back. | Added contact mangler (09-12), Added mangled usernames option (09-13) |
| Per-chat language | Contact local setting `chat_language`: None = auto, `off`, `auto`, or spell-checker code. Not synced. | Added per chat Language setting (08-29) |
| MSRP chat optional | `chat.enable_msrp_chat` default **False**; Chat button hidden when off; SIP MESSAGE pane is the path. | Refactored chat settings (08-26), Hide Chat button if MSRP is disabled (08-29) |
| No media proxy | Account `rtp.no_mediaproxy`, contact `no_mediaproxy` (debug UI) → `X-No-MediaProxy`. | Added no-mediaproxy setting (09-19) |
| Retry | 10 s heartbeat in the manager; `FAILED_LOCAL` older than 20 s requeued; on replay failed-local < 7 days requeued; at launch unsent conversations (60 days) reopened in background. | Fix resending failed local messages (08-28) |
| Dedup | `seen_message_ids` ring (10000), per-viewer ids, render check, DB unique key, journal-vs-live check. | Fix duplicate message display (08-28) |
| Logging | `[Message with <uri|instance id>]` prefix; one line per incoming message; journal summaries. | Replace SMS log prefix (09-17) |

Settings added or newly relevant:

| Setting | Scope | Default | Use |
|---|---|---|---|
| `chat.enable_msrp_chat` | global | False | MSRP chat on/off |
| `file_transfer.send_media_as_original` | global | False | preview default |
| `gui.mangle_contacts` | global | False | screenshot mode |
| `sms.file_transfer_url` | account (hidden) | None | upload base |
| `sms.activation_announced` | account (hidden) | False | first-sync note latch |
| `sms.history_url`, `history_token`, `history_last_id` | account | None | journal |
| `sms.enable_replication` | account | True | journal + read markers |
| `pstn.replace_leading_zero` | account | None | E.164 normalisation |
| `rtp.no_mediaproxy` | account | False | media relay opt-out |
| `chat_language`, `no_mediaproxy` | contact (local) | None / False | per contact |
| `modified_*`, group `kind` | contact/group (shared) | '' | device stamps, group identity |
| defaults `SIPMessageTranscriptFontSize`, `MessageGridColumns`, `MessagesPaneWidth`, `ContactListWidth` | UI | 13 / 3 / 480 / — | UI state |

---

## 20. Defects found in the macOS code (do not port)

| # | Issue | Where |
|---|---|---|
| 1 | `eval()` on journal IMDN content from the server (code execution on server data). Use `json`/`ast.literal_eval`. | `SMSWindowManager.py:2449` |
| 2 | Journal page file is unlinked even if applying it raised → data loss. | `_applyCachedJournals` |
| 3 | Message removal arriving before its target tombstones 0 rows and is never retried. | `tombstone_message` |
| 4 | First-sync watermark from the plan not implemented: backfilled incoming messages can come in unread; IMDNs skipped on first sync. | `syncIncomingMessage` |
| 5 | Two canonicalisers disagree on phone numbers (`_canonical_uri` does E.164, `_canonical_contact_uri` doesn't); mixing them in delete/expunge can purge history still claimed by a merged contact. | `ContactListModel` / `SMSWindowManager` |
| 6 | Calls/Tel removal blocked only in Edit Contact; "Remove From Group" and drag-out still work. | `BlinkGroup.__init__` |
| 7 | Stage-1 delete does write group membership to XCAP (docstring says nothing is sent). | `fileContactAsDeleted` |
| 8 | Merging a Bonjour row into an addressbook contact puts the instance id into XCAP. | `mergeContacts` |
| 9 | Exact-string `remote_uri` compares miss canonical/PSTN/Bonjour spellings for open viewers. | `_hasViewerFor`, `applyConversationRead` |
| 10 | Conversation-read may be announced twice from the tab path; read marker sent from a viewer goes `To:` the peer. | `SMSWindowController`, `_send_message` |
| 11 | Removing the token disables replication. | settings handler |
| 12 | `vacuum` after every delete. | `delete_message(s)` |
| 13 | Live no-viewer location path still decrypts at write time (contradicts "journal never decrypts"). | `_persistLiveMessage` |
| 14 | Map tile cache never expires, no size cap, no @2x tiles, no accuracy circle; `expire_time` unused. | `MapTileCache.py` |

Related Qt defects noticed (fix during the port): `OutgoingMessage._NH_SIPMessageDidFail` returns early because `__disabled_imdn_content_types__` is never empty, so SIP failures never reach the UI (`messages.py:541`); `if ['direction'] == 'incoming'` is always false (`messages.py:990`); all non-`text/` content types dropped (`messages.py:1384`); incoming conversation-read not persisted; call history rendered with `eval()`.

---

## 21. Patch timeline (messaging-relevant)

| Date | Patch |
|---|---|
| 08-26 | Added Location Framework · Refactor Messaging · Refactor Chat and History to use native widgets instead webview · Refactored chat settings |
| 08-27 | Added Audio recording · Added version 9.5.0 |
| 08-28 | Added Call button to SMS pane · Fix download detection · Only show track up to selected point by slider · Added attachment preview · Fix resending failed local messages · Fix duplicate message display · Fix take camera snapshot · Added Grab option to file transfer · Added PGP key escrow · Added Key Escrow plumbing · PGP Escrow fix · Log journal actions · Promote Messages group on top at creation time · Save activation anouncement · Fix sharing xcap attributes with mobile version · Tune SQL queries · Fetch last 5 years at 1st journal sync · Throttle presence subscription notifications · Set tls_name for sylk.link accounts |
| 08-29 | Improve messaging and contact avatars · Added centralized playback stop button · Hide Chat button if MSRP is disabled · Added per chat Language setting · File transfer fixes · Improve chat rendering performance |
| 08-30 | Added paste photo in inbut bar · Fixed scrolling label · Show location sharing state in contact · Fix loading image cache · Fix duplicate presence lines · Improve message history and category filtering · Fixes for call and audio recordings · Added video sending preview · Added Share extention |
| 08-31 | Reposition Bonjour group when activating account · Delete associated files whne delete fil transfer message · Implement conversation remove and Delete group · Implemented Bonjour messaging · File transfer fixes · Add multiple items selection in grid mode |
| 09-01 – 09-07 | Messaging fixes · Reuse original PGP key id as label · Bug fixes |
| 09-08 – 09-11 | Send message notification when addressbook changes · Work on CDR sync · Implemented CDR meessage bubble and PSTN contacts normalization · Fix matching PSTN display name with 00 · Fox posting notifications for Bonjour neighbors · Added call detail record modal · Filter out conversation read echos · Fix html links in dark theme · Theme switch fixes |
| 09-12 – 09-16 | Added contact mangler · Added mangled usernames option · Fix loading the views above message pane category bars · Fix open images in external viewer · Fix show conversation interval loaded labels · Only show keep scrolling messages if users already scrolled up · Track which devices changed a group or a contact |
| 09-17 – 09-18 | Don't announce addressbook copies tagged remote by sipsimple · Select sip uri in message pane if contact has more than one · Make PGP key clickable and add show xcap document for contact · Replace SMS log prefix · Added caption to images · Show last message on Contact tile 2nd line · Skip my own is-composing messages · Added message info panel · Chat fixes |
| 09-19 – 09-22 | Added no-mediaproxy setting · Use system font size for message input bar · Allow selection of multiple contacts · Make Delete multi stage · Added merge multiple selected contacts · Added codecs and UA to the call detail record |
| 09-24 – 09-30 | Keep cypher used by data import · Added Sylk data import plumbing · Added info button to image bubbles · Fix retry data transfer · Fix _canonical_contact_uri · Fix round corner · Keep the history, files and key of a contact removed on another device · Merge duplicate Messages contacts after the document is applied, keeping the lowest id |
| 10-01 | Render PDF files inline · No pdf caption |

Out of scope for this inventory (not messaging): video call window and recorder, screen-sharing pointer and request, AEC stats, lid-closed microphone detection, build scripts, Share extension packaging.
