# Blink Qt — Phase B: Messaging UI

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
     ├─ contact list (existing ContactListView; becomes the conversation switcher)
     └─ MessagePane (new)
         ├─ ConversationHeader      avatar, name, info line, URI switcher, account pill, lock, A−/A+, calendar, call buttons
         ├─ TranscriptStrip         loaded range, history note, scroll hint, search, filter chips
         ├─ TranscriptView          QListView, ScrollPerItem off, per-pixel scrolling, anchored prepend
         │    model:    ConversationModel  (rows = MessageItem; paging from MessageHistory)
         │    delegate: BubbleDelegate     (one painter per bubble kind; size cache per width)
         └─ Composer                text input, attach, record, smileys, reply/edit hint line
```

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
