# Blink Qt — Phase B: Messaging UI

**Status (2026-10-07):** B1–B6 implemented, patches 58–93, plus the patches listed under *Done alongside Phase B*. Where the code differs from the tables below, see *Implementation notes*. Video trim (part of 87) is not done; moving unread counting out of the chat window (the last part of 93) is left for a later patch.

Phase A made Blink Qt behave like Blink for macOS and Sylk Mobile on the wire, in XCAP and in storage. Phase B builds the user interface on the APIs it left behind (`MessageHistory` paging, categories, read state, tombstones, sidecars, CDRs, file cache, location storage). Reference: `blink-macos-messaging-inventory.md` (Inv §n) for behaviour, `patch-series.md` for what exists.

## Decisions

| Question | Decision |
|---|---|
| Where conversations live | **A pane in the main window**, as on macOS (Inv §1.1): contact list on the left is the conversation switcher, the conversation on the right. The separate window stays for audio, video and screen sharing only. |
| How the transcript is drawn | **Native Qt widgets**: a `QListView` over a conversation model, bubbles painted by a delegate. No QWebEngine in the message pane. |
| Order | Contact list (last message, typing, time, sorting) → the pane → transcript basics → media bubbles → filters and grid → actions and composer. |

Same rules as Phase A: one darcs patch per logical change, each leaves Blink working and is testable on its own; pure modules come with tests; new behaviour logs to the activity log; light and dark themes and the system font are followed from the first patch (`is_dark_theme`, `follow_theme`, `FontScaledSize`).

## Architecture

```
MainWindow
 └─ QSplitter
     ├─ contact list (existing ContactListView; the conversation switcher)
     └─ MessagePane                 pane.py
         ├─ ConversationHeader      header.py   avatar, name, address ▾, account pill, ■ stop, A−/A+, lock, calendar, 📍, call buttons
         ├─ TranscriptStrip         strip.py    loaded range, history note, scroll hint, search
         ├─ FilterBar               filters.py  category chips; Grid, columns, Select, Download All at the right
         ├─ QStackedWidget
         │    ├─ TranscriptView     view.py     QListView, per-pixel scrolling, anchored prepend
         │    │    model:    ConversationModel  model.py   (rows = MessageItem; paging from MessageHistory)
         │    │    delegate: BubbleDelegate     delegate.py (one painter per bubble kind; layout cache per id, width, font)
         │    └─ GridView           grid.py     tiles for pictures, videos, locations; multi-select bar
         └─ Composer                composer.py text input, attach menu, record, reply/edit hint line
```

Modules of `blink/messagepane/` as built:

| Module | What |
|---|---|
| `format.py` | Pure helpers (tested): summaries, file names, linkify, HTML sanitiser, bubble kind, day labels, delivery marks, auto-fetch limits, sizes, waveform bars, clock |
| `model.py` | `ConversationModel`, `MessageItem` (incl. `MessageItem.for_upload`), paging, live merge, search, jump, category filter |
| `delegate.py`, `view.py` | Bubbles (text, note, summary, image, PDF, file, call, audio, video, location), hit testing (`bubble_at`, `audio_hit`), menus |
| `media.py` | `MediaCache`: decode off the GUI thread, LRU by bytes, PDF page 1 (QtPdf), decoders per extension |
| `fetch.py`, `files.py` | Auto-fetch in view; where a message's file is, its info, its failure |
| `uploads.py` | Files being sent over HTTP shown as rows until the server's copy is in history |
| `audio.py`, `recorder.py`, `transcode.py` | One player app-wide (audio and video frames), waveforms; voice recorder; WAV → AAC (.m4a) with GStreamer |
| `video.py` | `VideoProbe`: poster and duration with GStreamer (gi), posters registered with `MediaCache` |
| `locations.py`, `position.py` | OSM tiles (`TileCache`), shares and trails (`LocationStore`), map window; current position (QtPositioning/GeoClue) |
| `attach.py`, `camera.py` | Attachment preview (crop, caption, smaller pictures); Take a Photo |
| `forward.py`, `info.py` | Forward dialog; message info panel |

- **`blink/messagepane/`** (new package): `model.py` (ConversationModel, MessageItem), `delegate.py` (BubbleDelegate and kind painters), `pane.py` (MessagePane, header, strip), `composer.py`, `media.py` (thumbnails, posters, waveforms, PDF page 1; decoded off the GUI thread, cached by path+mtime+size), `format.py` (pure: day labels, times, sizes, HTML sanitiser, linkifier; tested).
- **Model**: rows come from `MessageHistory.get_messages(remote, before, category, limit)`; live rows arrive through the existing notifications (`BlinkGotMessage`, `BlinkGotHistoryMessage`, delete/tombstone, state changes) keyed by message id. One row per message id; a duplicate only updates state. Sidecars (captions, replies, peaks, location ticks) are attached to their target row, never shown as rows.
- **Delegate**: paints text with `QTextDocument` (sanitised HTML / linkified plain text) and media from pixmaps the model hands it; heavy content (video playback, map pan/zoom, audio scrub) becomes a real widget only while the bubble is under the mouse or playing (`openPersistentEditor` on demand), otherwise it is painted from a cached frame. Size hints cached per (id, width, font).
- **Conversation state** per key (scroll position, unsent text, filter, loaded range) kept in the pane, as macOS keeps a viewer per conversation (Inv §1.2).
- **Dependencies to confirm before B4**: `PyQt6.QtMultimedia` (video, audio playback, recording), `PyQt6.QtPdf` (PDF page 1; packaged separately on some distributions), `QtPositioning` (current location; optional, menu item disabled with the reason when absent).
- **What goes away at the end**: the chat window's message transcript (`ChatWidget`, `chat/template.html`) for SIP MESSAGE conversations. MSRP chat sessions keep their separate window and widget (decided).

---

## B1 — Contact list

Independent of the pane: these only read history, so they can go first.

| # | Patch name | Scope | Verify |
|---|---|---|---|
| 58 | Show the last message on the contact's second line | Messages group rows: newest text message across all spellings of the contact's addresses (PSTN variants, Bonjour id), decrypted if needed (plaintext written back), skipping keys, synthetic location/meeting texts and pure-emoji reactions; whitespace collapsed, 100 chars with `…` (`conversation_preview`, `last_text_messages`); files, locations and calls leave the previous text; second line order: typing → sharing location → last message → contact detail (Inv §12) | Preview follows new and removed messages, live and after a journal sync |
| 59 | Show is typing… on the contact's second line | "✎ is typing…" from is-composing state per canonical key, expiring after refresh + 1 s; own echoes ignored (Inv §5.4, §12) | Typing on mobile shows on the row and clears |
| 60 | Show the time of the last message or call on the contact row | Right-aligned: `HH:MM` today, "Yesterday", weekday < 7 days, `%d %b` this year, else `%d/%m/%y`; Messages group: last message, Calls/Tel: last call; name and second line elide before it (Inv §12) | Times correct across midnight and year end |
| 61 | Sort Messages by last message and Calls/Tel by last call | Messages group by last message time, Calls/Tel by last call time (stable: alphabetical, then time descending), selection kept on re-sort; per-row updates, coalesced during journal import, never a full reload (Inv §12, §14.1) | New message moves the contact to the top without losing the selection; a 10 000-message sync does not flicker |

## B2 — The pane

| # | Patch name | Scope | Verify |
|---|---|---|---|
| 62 | Added the message pane to the main window | `QSplitter` with the contact list and an empty `MessagePane`; toggle from the menu and Ctrl+4; widths persisted (list ≥ 274, pane ≥ 320, default 480); window grows/shrinks by the pane width; **never reopened at launch** (Inv §1.1) | Pane opens/closes, widths survive restart, opening at start does not mark anything read |
| 63 | Switch the pane with the contact selection | Selecting a contact switches the pane, never opens it; empty state "Select a contact to see messages"; which URI a contact opens on (last URI that carried a message, else default) (Inv §1.2, §1.3) | Selection follows; contact with two URIs opens on the last used |
| 64 | Added the conversation header | Avatar (photo or initials on colour), name, info line ("is typing…"), call and video buttons, encryption lock with its menu (PGP key id, lookup, send my key) (Inv §1.3) | Header follows selection and typing; calls start from the conversation's account |

## B3 — Transcript basics

| # | Patch name | Scope | Verify |
|---|---|---|---|
| 65 | Added the conversation model with paging | `ConversationModel` over `get_messages`; 50 renderable rows per page (`renderable_cutoff`), anchored prepend on scroll up, "more available" probe; live inserts in timestamp order (Inv §3) | 10 000-message conversation opens at once; scrolling up loads 50 more without jumping |
| 66 | Draw text bubbles | `BubbleDelegate`: incoming/outgoing fills for light and dark, turn grouping (avatar and name at the start of a run), sender time, selectable text, sanitised HTML and linkified plain text, allow-list of renderable content types, system notes (Inv §2.1) | No raw JSON ever drawn; links open; dark theme readable |
| 67 | Day dividers and delivery state | "Today", "Yesterday", weekday, date; ✔ delivered, ✔✔ displayed, 🕑 pending, failed fill; pruned on delete (Inv §2.1) | Dividers correct across midnight; ticks update live |
| 68 | Loaded range, history note and search | Strip: "N messages, 12 Jul 08:00 – 26 Aug 17:40", loading notes, "hold up-scrolling…" hint only after the user scrolled up; search field with SQL search and highlighted hits (Inv §3) | Range matches what is shown; search finds old messages |
| 69 | Read state from the pane | Visible = selected + pane shown + window active; becoming visible marks read, sends IMDN displayed and the cross-device read marker; losing visibility pauses (Inv §5.1) | Badges clear only when the pane really shows the conversation |
| 70 | Jump to date | Calendar menu in the header: years → months → days with message counts; jump loads the page ending that day (Inv §1.3) | Jump to a year-old day shows it |

## B4 — Media bubbles

| # | Patch name | Scope | Verify |
|---|---|---|---|
| 71 | Added off-thread media decoding and caching | `media.py`: thumbnails at bubble size, natural size, cache by path+mtime+size, decode in a worker, repaint on ready | Scrolling a picture-heavy conversation stays smooth |
| 72 | Auto-fetch files in view | Only bubbles in the viewport, 0.3 s coalescing; limits image 8 MiB, PDF 10 MiB, video 20 MiB and ≤ 7 days, audio/other never; skipped when encrypted without a key or failed before (`.failure.json`); progress in the bubble (Inv §2.3) | Big files wait for a click; failures are not retried on every scroll |
| 73 | Image bubbles | Inline, max height 320 (640 for large sources), min width 120; click opens; captions under the picture (`label` sidecar) (Inv §2.2, §2.4) | Captions from mobile shown |
| 74 | File and PDF bubbles | File: system icon, "📎 name", "type · size", red "⚠ error"; PDF: page 1 inline (QtPdf), pill "PDF · N pages · size" (Inv §2.2) | Expired transfer shows its error; PDF page renders |
| 75 | Call record bubbles | CDR: direction arrow, label, "duration — reason", video marker, colours by outcome; info opens a call details dialog (Inv §2.2, §17) | Calls from mobile and macOS show once, with the right outcome |
| 76 | Audio and voice note bubbles | Player with 48-bar waveform from `peaks` (else measured), seek by click/drag, call-recording titles; one clip at a time app-wide, header ■ stop (Inv §2.2, §18.3) | Voice note from mobile plays with its waveform |
| 77 | Video bubbles | Poster off the GUI thread, duration, play badge, inline playback with transport row, open in system player; unplayable → file bubble (Inv §2.2) | Mobile video plays inline |
| 78 | Location bubbles | OSM tiles with User-Agent, disk cache `map_tiles/z/x/y.png`, pin, live trail with slider, zoom/pan/recentre, system notes for start/stop/meet-up; contact row "📍 is sharing location…" (Inv §11) | Live share from mobile shows a moving trail |

## B5 — Filters and grid

| # | Patch name | Scope | Verify |
|---|---|---|---|
| 79 | Category filter bar | Chips All + present categories (`present_categories`, links probe), shown when ≥ 2; choosing one reloads from SQL 50 at a time with sidecars (Inv §4.2) | Filter "image" pages through all pictures, not only the loaded ones |
| 80 | Grid mode for pictures, videos and locations | 2–6 columns (default 3, persisted), 4:3 centre-cropped tiles, month dividers, size pill, info button; "Download all" in the viewport for video (Inv §4.3) | Grid follows width; months split correctly |
| 81 | Multi-select in the grid | Checkbox per tile, shift-click extends; bar "N selected · Forward… · Delete · Done"; delete with remote option for own items; drag of ticked local files (Inv §4.3) | Delete of 10 tiles asks once |

## B6 — Actions and composer

| # | Patch name | Scope | Verify |
|---|---|---|---|
| 82 | Added the composer | Multi-line input in the system font, Enter sends, Shift+Enter newline, is-composing sent, A−/A+ font size for transcript and composer (persisted), paste text; drop of files anywhere on the conversation (Inv §1.4) | Typing indicator reaches mobile; font size survives restart |
| 83 | Per-message actions | Hover header: copy, open/save-as, reply, info, delete (with "for X too" on own messages, deleting a transfer deletes its file); context menu with the same (Inv §2.5) | Delete for both removes on mobile |
| 84 | Reply | Reply mode hint line, `reply` sidecar sent before the reply, quote block in the reply bubble, click scrolls to and flashes the original (or loads it) (Inv §2.5) | Replies from mobile show their quote |
| 85 | Edit message and caption | Edit own text (delete + resend at the original time), Edit Caption for own pictures/videos (`label` sidecar) (Inv §2.4, §2.5) | Edited message keeps its place |
| 86 | Message info panel | Message, Delivery, Replies, File transfer, Location, Storage, Related sections; selectable values (Inv §2.6) | Panel shows stored vs shown state |
| 87 | Attachments and preview | Attach menu (choose files, paste picture, take screenshot); preview with crop for a picture, trim for a video, caption field, "send original" (Inv §1.4, §18.2) | Picture sent downscaled with caption; mobile shows both |
| 88 | Take a screenshot to send | "Take screenshot…" in the attach menu through the XDG desktop portal (`org.freedesktop.portal.Screenshot`, interactive): the desktop's own picker chooses screen, window or area, on Wayland and X11 alike; the file goes through the attachment preview. Without the portal (other X11 desktops) fall back to a Qt grab of the screen plus an area-selection overlay. Logged with the route taken (Inv §1.4) | GNOME shows its picker; the shot arrives in the preview and is sent |
| 89 | Voice recording | Record button, recorder bar replacing the composer (level, clock, cancel), preview with waveform, send with `peaks` sidecar; one recorder app-wide, max 600 s (Inv §18.1) | Voice note plays on mobile with waveform |
| 90 | Forward | From the grid selection and the message menu: 12 most recent conversations (Inv §4.3) | Forwarded picture arrives as a new transfer |
| 91 | Send and request location | Header 📍 when the account has a SylkServer: "Send current location" (QtPositioning, 20 s timeout), "Request location" (Inv §11) | Mobile shows the pin; request appears there |
| 92 | URI switcher, account pill and account confirmation | Header ▾ for contacts with several addresses, "From ‹account›" with several accounts, first-message account question for unmatched domains (Inv §1.3, §1.4) | Switching address switches conversation |
| 93 | Retire the HTML transcript for SIP messages | Messages window no longer opens for SIP MESSAGE conversations; notifications and "open conversation" go to the pane; MSRP chat in calls unchanged | No conversation reachable only through the old window |

---

## Implementation notes

Where the code differs from the tables above.

| # | Note |
|---|---|
| 58 | Second line order as built: typing → sharing location (78b) → last message → contact detail. |
| 62 | Toggle and Ctrl+4; opening and closing the pane are logged. Selecting a contact while the pane is closed loads nothing; the conversation is loaded when the pane opens. |
| 66 | The delegate paints every kind itself; no persistent editors are used (`openPersistentEditor` was not needed). |
| 69 | Read path: `BlinkMessagePaneDidReadConversation` clears the contact badge. Unread counts are still posted by the chat window's message handling (see 93). |
| 72 | Audio is fetched too: up to 10 MiB and 7 days old, like video (20 MiB). |
| 76 | One player for the whole application (`AudioPlayer`), also used for video (77). Voice notes are sent as AAC in .m4a (GStreamer via python3-gi, `transcode.py`), the WAV when no AAC encoder is installed. |
| 77 | Inline playback paints the player's frames (QVideoSink) inside the bubble, with the transport over its bottom; no video widget. Posters and durations come from GStreamer (`video.py`). |
| 78 | Bubble: static map with pin, trail and meeting point; pan, zoom, recentre and the trail slider are in the map window a click opens (`LocationWindow`). Tiles from `{a,b,c}.tile.openstreetmap.de`, 4 at a time, 15 s timeout, retried after 60 s. The contact row line ("⌖ is sharing location…", ⌖ because 📍 draws as an empty box on common Linux fonts) came as its own patch, 78b (`ConversationLocations` in `history.py`). History posts `BlinkMessageHistoryLocationDidStore` for every stored tick. |
| 80 | Grid opens at the newest month and loads older months when scrolled up, like the transcript. |
| 81 | Remote delete is offered only for messages one sent, as for a single message. |
| 86 | Also a Clients section and a User agent row (see *Done alongside*); values are shown without `sip:` and lists comma separated. |
| 87 | Pictures are made smaller unless "Send original": at most 2048 px on the long side, JPEG 85 (PNG with transparency); a crop always makes a new file. No video trim (needs an H.264 encoder; x264 is not in the dependencies). Every file sent from the pane goes through the preview (choose, drop, paste, screenshot, photo). |
| 91 | 📍 shown when the account has a journal URL (SylkServer). Positioning asks GeoClue directly with desktop id `blink`; a failure opens a dialog with the reason; the greyed menu item says why. The request is not drawn in the transcript (stored as a signal without a category). |
| 92 | The chosen account is kept per conversation for the session, not across restarts. A first message to a domain one of the accounts is in goes from that account without asking. |
| 93 | Message-only sessions are hidden from the chat window's list and never select it; Window → Chat Window opens the pane when there is no MSRP, video or screen sharing session; a call's "Send Messages" opens its conversation in the pane. The chat window still handles those sessions' messages in the background (unread counts, queued receipts); moving that out would let `ChatWidget` stop rendering SIP messages altogether. |

## Done alongside Phase B

| Area | Patch |
|---|---|
| File transfers | HTTP uploads shown in the pane while they upload (✓ when done, ⚠ with retry, cancel); the File Transfers window and its auto-opening are for MSRP transfers only |
| Messages | Messaging log lines for sent, received and disposition with message ids; journal duplicates no longer mark read messages unread; live IMDN not stored as an unknown type |
| Receipts | An `error`/`failed` receipt no longer moves a delivered or displayed message back (Blink Qt, Blink Cocoa `HistoryManager` and `markMessage`, Sylk Mobile `updateMessageState`); Blink Cocoa: displayed receipts sent again (EventQueue pause counter) |
| User agents | SylkServer sends `X-Sylk-User-Agent` (the web or mobile client) with messages and receipts; Blink Qt records the client of every message and receipt (`message_agents` table, no schema change of `messages`) and shows it in Info and the logs; messages known only from the journal have none |
| Composer | Take a Photo (camera) in the attach menu |
| Audio devices | Devices menu: combined input+output devices on top select both sides; each side checked against its own list on refresh, the alert device included |
| Transcript | Clicks and right clicks only inside the bubble |
| Packaging | Depends: python3-pyqt6.qtmultimedia, python3-pyqt6.qtpdf, python3-gi, gir1.2-gstreamer-1.0, gstreamer1.0-plugins-base/-good, gstreamer1.0-libav; Recommends: python3-pyqt6.qtpositioning, libqt6positioning6-plugins, geoclue-2.0 |

## Left after Phase B

- Video trim in the attachment preview (needs an H.264 encoder in the dependencies).
- Unread counting and receipts out of the chat window, so it renders MSRP chat only.
- Messages SylkServer writes after an HTTP upload carry no `X-Sylk-User-Agent` (`web.py`).
- Sylk Mobile sent `error` display receipts for .m4a voice notes it had received: to be checked with mobile logs.
- 57 (Sylk data import), from Phase A.

## Ordering notes

- 58–61 first: they work on the existing contact list and only read history, so they are useful before the pane exists. 61 after 58 and 60 (sorting uses the same last-message and last-call times).
- 62–65 next: everything after draws into the pane and the model.
- 71 before 72–78 (all media goes through the decoder and its cache); 72 before 73–77.
- 79 before 80–81 (the grid is a filtered view); 82 before 84–85 and 87–89 (they use composer modes); 87 before 88 (the screenshot goes through the preview).
- 93 last, when the pane covers everything the old window did for SIP messages.

## Decided

- MSRP chat stays in its separate window.
- Screenshots go through the desktop portal (patch above), so they work on Wayland too.
- After Phase B: contact mangler (screenshot/demo mode), per-chat language, Edit Contact "XCAP" pill (Inv §14.5, §19).
- Remote delete stays limited to one's own messages.
- User agents are not stored in the server journal (neither a new column nor the metadata column).
