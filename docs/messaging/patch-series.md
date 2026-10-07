# Blink Qt — Phase A Patch Series

**Status (2026-10-07):** A1–A5 implemented through patch 56; 57 (Sylk data import) deferred. Phase B (the messaging UI, patches 58–93) is in `ui-plan.md`, with its status and notes. Every row was checked against the code on that date. Where the implementation differs from the plan, see *Implementation notes* below; patches recorded alongside the series, mostly UI and logging, are listed at the end.

One darcs patch per logical change, in recording order. Each patch leaves Blink Qt running and is testable on its own. Section references point to `porting-plan-protocol.md` (Plan §) and `blink-macos-messaging-inventory.md` (Inv §).

Rules for every patch:

- Touches only what its name says; no drive-by refactors.
- Schema changes bump `MessageHistory.__version__` by one step each, so a partially applied series still migrates cleanly.
- New behaviour logs to the Activity log (after patch 1) with its subsystem prefix.
- Pure modules come with their tests in the same patch.

Columns: **Files** = main files touched · **Verify** = what to check before recording.

---

## A1 — Foundation

| # | Status | Patch name | Files | Scope | Verify |
|---|---|---|---|---|---|
| 1 | done | Added Activity log | `blink/logging.py`, `blink/configuration/settings.py` | `ActivityLog` singleton (`info/warning/error/exception/debug`), always writes `<ApplicationData>/logs/activity.txt`, backlog 5000 while no window; `logs.activity_debug` setting gates debug lines (Plan §1.3) | `activity.txt` created at start, survives non-ASCII |
| 2 | done | Log SDK version, codecs and accounts at start | `blink/__init__.py` | Same startup lines as macOS `SIPManager`/`BlinkAppDelegate`: Blink/Python/Qt versions, SDK + core + PJSIP version, data dir, device id, core/configured audio and video codecs, devices, Bonjour availability, account activate/register/unregister/fail, TLS transport, XCAP root/capabilities/errors, IP change | Lines present in `activity.txt` after start |
| 3 | done | Added Activity tab to Logs window | `resources/logs_window.ui`, `blink/logswindow.py` | Tab first, 150 ms batched flush, cap 4000 pending, detach when hidden | Lines appear live; closing window does not lose lines in file |
| 4 | done | Added script to reset Blink state for import tests | `scripts/reset_state.sh` | Plan §1.5 | Fresh start re-syncs from scratch; `--keys` removes keys |
| 5 | done | Fix reporting of failed SIP messages | `blink/messages.py` | Remove early return in `OutgoingMessage._NH_SIPMessageDidFail` (Plan §2) | 404 target → message shows failed; retry picks up `failed-local` |
| 6 | done | Fix delivered notification for synced messages | `blink/messages.py` | `['direction']` → `history_message.direction` | Sync of unseen incoming sends `delivered` |
| 7 | done | Fix removing message history of an account | `blink/history.py` | `account=` → `account_id=` | Removing an account clears its rows |
| 8 | done | Run message history migrations in sequence | `blink/history.py` | Rewrite `_check_table_version` as chained `if version < n:` steps, each logged with rows touched and duration; no schema change | Upgrade from v1, v2, v3 DB copies reaches v4 |
| 9 | done | Added message history columns for read state, tombstones and metadata | `blink/history.py` | v5: `read, category, has_link, metadata, related_msg_id, related_action, deleted, deleted_time, journal_id, sip_callid, media_type, cpim_from, cpim_to, cpim_timestamp, private, expire_time` + indexes (Plan §3.0); no backfill | `PRAGMA table_info`, indexes present; app unchanged |
| 10 | done | Added pending message removals table | `blink/history.py` | `pending_removals(message_id, account_id, remote_uri, removed_at, source)` | Table created on fresh and upgraded DB |
| 11 | done | Backfill read state, tombstones and CPIM fields | `blink/history.py` | v6: `read=0` for renderable incoming not displayed; `deleted` from `state='deleted'`; `media_type`, `cpim_from/to` by direction | Unread totals equal the old `get_unread_messages()` minus api/pgp rows |
| 12 | done | Added phone number normalisation | `blink/pstn_normalize.py`, `tests/test_pstn_normalize.py` | Port of macOS `pstn_*`, `canonical_pstn_uri`, `same_phone_number`, `pstn_uri_spellings*` (Inv §13.3) | Test vectors pass |
| 13 | done | Added replace leading zero PSTN account setting | `blink/configuration/account.py` | `pstn.replace_leading_zero` (Digits, nillable) | Setting persists |
| 14 | done | Added canonical URI helpers | `blink/uris.py`, `tests/test_uris.py` | `canonical_uri` (single canonicaliser), `bare_instance_id`, `is_placeholder_uri`, `illegal_uri`, `is_fileable_address` | Tests pass |
| 15 | done | Added message envelope and category helpers | `blink/message_envelopes.py`, `tests/test_envelopes.py` | Content-type constants, file-transfer envelope + RCS, `classify_category`, `has_link`, label/reply/peaks/call_recording envelopes, `conversation_preview`, `public_key_id` | Tests pass |
| 16 | done | Added call detail record helpers | `blink/message_envelopes.py`, tests | `build_call_record`, `merge_call_records` (source ranks), `call_summary`, `call_was_missed` | Merge-rank tests pass |
| 17 | done | Added location payload module | `blink/location.py`, `tests/test_location.py` | Port of `SylkLocation.py` (Inv §11) | v1/v2/legacy vectors pass |
| 18 | done | Backfill message categories | `blink/history.py` | v7: text in one UPDATE, file transfers in chunks of 500 with commit, `has_link` for text; logs counts and unclassified | Category counts logged; re-run is a no-op |
| 19 | done | Convert call history to call detail records | `blink/history.py`, `blink/chatwindow.py` | v8: pickle `calls_history` + `application/blink-call-history` rows → CDR rows (`source='migrated'`, category `call`); renderer reads CDR, `eval()` removed | Calls still listed in chat; no `eval` left |
| 20 | done | Key Bonjour conversations by neighbour instance id | `blink/history.py`, `blink/messages.py`, `blink/sessions.py`, `blink/contacts.py`, `blink/uris.py`, `blink/chatwindow.py`, `blink/mainwindow.py` | v9: `<id>@local` → bare id, `account_id='bonjour@local'`; new rows keyed the same; Bonjour account for neighbours, one address per neighbour (transport ranking), names remembered in `bonjour_neighbours.json`, instance id shown instead of the transport address, nothing sent to placeholders, unread keyed by instance id | Bonjour chat history intact after upgrade; one conversation per neighbour |
| 21 | not needed | ~~Do not store presence changes in message history~~ | — | Not needed in Qt: availability rows were a macOS-only local table entry; Qt never wrote them and skips CPIM status messages before storage | — |
| 22a | done | Added message read state | `blink/history.py`, `blink/chatwindow.py` | `read` set at insert, `mark_conversation_read`, `unread_counts` replace `state != 'displayed'`; read on open, on arrival in view, on conversation-read from another device | Unread counts survive restarts and clear on open |
| 22b | done | Added message tombstones | `blink/history.py` | `tombstone_message` (related rows, sidecars), `tombstone_conversation`, `restore_conversation`, `deleted_conversations`, apply `pending_removals` | Removal before target applies on arrival |
| 22c | done | Added message history paging and previews | `blink/history.py` | `last_message_times`, `last_text_messages`, `present_categories`, `get_messages(category, before, limit)`, `related_messages` | Unit-level calls against a test DB |
| 22d | done | Added message body and decryption updates | `blink/history.py` | `update_message_body(merge=)`, `update_decrypted_message`, `move_conversation`, vacuum at idle/startup only | Location trail merge; category stamped after decrypt |

## A2 — Journal and live messages

| # | Status | Patch name | Files | Scope | Verify |
|---|---|---|---|---|---|
| 23 | done | Rework history token request | `blink/messages.py` | 30 s rate limit per account, `tls_name`, 401 → re-request after 30 s, triggers (Plan §4.1) | Token obtained over TLS; no request storm |
| 24 | done | Download the message journal in pages to a local cache | `blink/journal.py`, `blink/messages.py` | First sync `?since=now−5y`, cursor after file write, 200-page cap, per-account guard; pages still applied with the existing per-message code | `journal/<account>/` files; cursor advances only after write |
| 25 | done | Apply journal pages through the content type dispatch | `blink/journal.py`, `blink/history.py` | Plan §4.3 dispatch, bulk mode, no decryption, no session creation, IMDN skipped on first sync, file deleted only after successful apply | 10K-entry account imports; no sessions opened |
| 26 | done | Log journal import statistics | `blink/journal.py` | Per-page lines, progress every 250, per-type and per-contact summaries, DB check, `logs/import-<account>-<ts>.json` (Plan §1.4) | Summary equals DB counts; two runs diff cleanly |
| 27 | done | Defer message removals until the target arrives | `blink/journal.py`, `blink/history.py` | Use `pending_removals`; resolve on insert | Removal before target ends tombstoned |
| 28 | done | Store all incoming message content types | `blink/messages.py` | Drop the `startswith('text')` filter; unknown types stored inert (`read=1`); `seen_message_ids` ring 10000 shared with journal | Unknown type stored, not shown |
| 29 | done | Store message metadata companions | `blink/messages.py`, `blink/journal.py` | `reply`, `label`, `peaks`, `call_recording` linked via `related_msg_id`, never unread | Caption from mobile stored and linked |
| 30 | done | Store location messages and merge trails | `blink/messages.py`, `blink/journal.py`, `blink/history.py` | `related_*`, `category`, `metadata` from envelope summary; `update_message_body(merge=)` | Live share from mobile = one row with growing trail |
| 31 | done | Store call detail records from other devices | `blink/messages.py`, `blink/journal.py` | Own account only, not this device, merge by `(account, sip_callid)` | Call on mobile appears once in Qt |
| 32 | done | Persist conversation read state across devices | `blink/messages.py`, `blink/mainwindow.py` | Incoming `sylk-conversation-read` → `mark_conversation_read`; unread seeded from `read` | Read on mobile stays read after Qt restart |
| 33 | done | Send conversation read markers with the device id | `blink/messages.py` | `{"contact","device_id"}` to own account; own echo swallowed silently | macOS/mobile clear; no echo log in Qt |
| 34 | done | Tombstone removed messages and conversations | `blink/messages.py`, `blink/history.py` | Message remove by target id + sidecars; conversation remove up to timestamp; revival by newer message; own echo 60 s | Rows hidden, not deleted; revival works |
| 35 | done | Send plain text unless the message has formatting | `blink/chatwindow.py`, `blink/messages.py` | `text/plain` unless rich input | Mobile shows no HTML |
| 36 | done | Align outgoing message headers with Blink for macOS | `blink/messages.py` | No CPIM for `sylk-api-*`/pubkey; `X-Sylk-Skip-Journal` for OTR; read `agp.Metadata` into `metadata` | Trace shows headers as on macOS |
| 37 | done | Ignore own is-composing echoes | `blink/messages.py` | Replicated outgoing composing ignored; send with refresh 60 | No self "typing" |

## A3 — Contacts and XCAP semantics

| # | Status | Patch name | Files | Scope | Verify |
|---|---|---|---|---|---|
| 38 | done | Share contact attributes with Blink for macOS and Sylk Mobile | `blink/configuration/addressbook.py` | Namespace `ag-projects:sipsimple`; one-time copy from old bag; `auto_answer` shared; add `organization, disable_smileys, modified_*`, local `chat_language, silence_notifications, no_mediaproxy, public_key*`; URI `position` | Attribute set on macOS visible in Qt and back |
| 39 | done | Added group kind and reserved group resolution | `blink/configuration/addressbook.py`, `blink/contacts.py` | `kind` attribute; resolve kind → name → reserved id; `stamp_group_kinds` fills missing only | Logged stamps; no overwrite of other clients' kinds |
| 40 | done | Stamp addressbook changes with the device that made them | `blink/addressbook_origin.py`, `blink/__init__.py` | Port of `AddressbookOrigin.py`; install on Contact/Group; reason stack | `modified_by` = Qt instance id after an edit |
| 41 | done | Log addressbook changes on XCAP reload | `blink/contacts.py` / `blink/logging.py` | Origin diff, ETag, counts, members per group (Plan §1.4) | macOS edit logged "by ‹device›" |
| 42 | done | Use the shared Messages group | `blink/contacts.py` | Real `_messages` group replaces virtual `__messages`; filed from history; never Bonjour; promoted on creation and after first sync | Same group membership as macOS |
| 43 | done | Added Calls, Tel and Deleted groups | `blink/contacts.py` | `ensure_group` gated on XCAP loaded; membership changes refused in the model for Messages/Calls/Tel/Deleted | Groups appear once; drag-out refused |
| 44 | done | Use canonical URIs for conversations and contact matching | `blink/messages.py`, `blink/contacts.py`, `blink/mainwindow.py` | `blink.uris.canonical_uri` everywhere keys are built | `0031…`, `+31…`, `+31…@domain` = one conversation |
| 45 | done | Store new phone number contacts in E.164 | `blink/contacts.py` | Contacts created from messages/calls | New tel contact stored `+…`, type `tel` |

## A4 — Healing, deletion, notifications, keys

| # | Status | Patch name | Files | Scope | Verify |
|---|---|---|---|---|---|
| 46 | done | Repair contacts after the addressbook is loaded | `blink/contacts.py` | XCAP reload + 15 s, once per process, notifications suppressed: conference domain, E.164, echoed names, URI dedup, file into Tel/Conference; each action logged | Repairs logged with reason; stamped `repair` |
| 47 | done | Merge duplicate Messages contacts keeping the lowest id | `blink/contacts.py` | Union-find on canonical URI; name/URI donation; after notifications resume | Qt, macOS, mobile keep the same id |
| 48 | done | Keep the history of contacts removed on another device | `blink/contacts.py`, `blink/history.py` | `data.remote` → keep; local delete purges only unclaimed URIs, never Bonjour | Remote delete keeps messages |
| 49 | done | Two-stage contact delete | `blink/contacts.py`, `blink/history.py` | Stage 1 → Deleted + tombstones; stage 2 → XCAP delete + purge; restore | `_deleted` visible on macOS |
| 50 | done in 49 | Announce conversation removal on permanent delete | `blink/messages.py` | `sylk-api-conversation-remove` from replicating accounts; echo swallowed | Mobile drops the conversation |
| 51 | done | Notify other devices when the addressbook changes | `blink/addressbook_notify.py`, `blink/contacts.py`, `blink/messages.py` | Port of `AddressbookNotify.py`; send/receive with debounce/fuse/backoff; not for remote-applied docs | One tick per edit; macOS fetches |
| 52 | done | Never write Bonjour neighbours to the addressbook | `blink/contacts.py`, `blink/messages.py` | Guards on create, key filing, merge, purge | XCAP document has no instance ids |
| 53 | done | Escrow the PGP private key in the own XCAP contact | `blink/key_escrow.py`, `blink/messages.py`, `blink/contacts.py` | Port of `KeyEscrow.py`; restore when no local key; repair once per session; key generation waits for escrow answer | Fresh install with `--keys` restores key without prompt |

## A5 — Files and import

| # | Status | Patch name | Files | Scope | Verify |
|---|---|---|---|---|---|
| 54 | done | Learn and derive the file transfer upload URL | `blink/configuration/account.py`, `blink/messages.py` | `sms.file_transfer_url` from first incoming transfer, else from history URL | URL logged |
| 55 | done | Upload files over HTTP | `blink/sessions.py`, `blink/messages.py` | POST to `<base>/<sender>/<receiver>/<id>/<name>`, PGP `.asc` ≤ 50 MB, `Apikey`; MSRP kept for Bonjour / no server | Mobile receives and decrypts |
| 56 | done | Cache downloaded files per peer and transfer | `blink/sessions.py`, `blink/history.py` | `file_transfers/<account>/<peer>/<transfer_id>/`; failure classes; `error` in envelope; 404/410 → delete locally if absent | Gone transfer removed; retry works |
| 57 | deferred | Import messages and files from a Sylk Mobile export | `blink/data_import.py`, `blink/messages.py` | Port of `DataImport.py`; fresh announcement only; add-only | Import fills gaps only |

---

Ordering notes:

- 1–4 first so every later patch is observable.
- 9–11 before 18–21 (columns before backfills); 12–17 before 18 (classifier) and 19 (CDR helpers).
- 24 then 25 keeps the journal working at every step: 24 changes only how pages are fetched.
- 38 before 53 (escrow lives in the shared attribute bag); 44 before 46–47 (healing relies on one canonicaliser).

---

## Implementation notes

Where the code differs from the table above.

| # | Note |
|---|---|
| 8 | Steps are `MessageHistory._upgrade_to_v<n>()`, run one at a time by `_check_table_version`; the version is stored after every step, so an interrupted upgrade resumes. The messages table is at v11; Phase B added no messages schema step, only a separate `message_agents` table (created when missing). |
| 12 | Module `blink/pstn_normalize.py` (not `pstn.py`); `sessions._normalize_uri` applies the account dial plan (`pstn_dial_username`) and logs `[call] Dial plan`. |
| 19 | No `eval()` left in `chatwindow.py` or `history.py`. |
| 29, 30 | Schema v10 files metadata companions against their message, v11 location ticks against their share. |
| 43 | Also a Conference group (kind `conference`), filed when a conference room session starts; membership of Messages, Calls, Tel, Conference and Deleted is managed by Blink only. Deleted is hidden while empty. |
| 49 | Delete moves a contact to Deleted from any group and from search; "Remove from Group" is for user groups only; Undo Delete was removed (two-stage delete replaces it); a deleted contact offers only Restore and Delete Permanently. |
| 50 | Implemented with 49: `MessageManager.announce_conversation_removal` runs on Delete Permanently. |
| 51 | `AddressbookNotifier` (in `contacts.py`); healing (`ContactRepair`, `GroupKindStamper`) and key escrow writes run inside `quiet()` and are not announced; the decision is taken on the saving thread (file-io). |
| 52 | Most guards already existed (Messages/Calls filing, trash, purge, local Bonjour names); the patch added `is_bonjour_address` for the contact editor, Calls and Messages filing, and removed Edit/Delete for neighbours in search. Only `contacts.py` changed. |
| 53 | `KeyEscrowManager` lives in `messages.py`; no `contacts.py` change. When no contact carries the account's own address, one is created (as Sylk Mobile does) and the escrow is written on the next reload. No "save key on server" menu item yet. |
| 54 | URL helpers in a new `blink/file_transfer.py` (shared by 55 and 56). |
| 55 | `BlinkFileTransfer` gets an `http` route (`_upload` / `_post_file`), keeping progress, retry and cancel; since Phase B an HTTP upload is shown in the message pane (`blink/messagepane/uploads.py`) and the transfers window is for MSRP transfers only. The SylkServer echo of our own upload is what puts the sent file in history. Known gap: an encrypted upload that fails cannot be retried (retry re-reads the `.asc` path). |
| 56 | Path built by `configuration.datatypes.sylk_file_path`; files under the old `downloads/<id>/` are still found. Permanent and gone failures are kept in `<transfer folder>/.failure.json`, not in the envelope: Qt stores incoming transfers as RCS XML, which has no field for it. A gone transfer not on disk is tombstoned and removed from the open chat. |
| 57 | Deferred. |

## Done alongside the series

Recorded as separate patches during Phase A; not part of the numbered plan.

| Area | Patch |
|---|---|
| SDK (python3-sipsimple) | Registration: on 408 try the next route, per-route timeout; addressbook: do not apply a fetched document while local XCAP changes are still being sent |
| Calls | `[call]` activity lines for new/started/ended/failed sessions; dial plan applied (`replace_leading_zero`); Join Conference moved to the Call menu, dialog titled "Join Conference" with "Room:" |
| Logs | RTP Media tab in the Logs window and `logs/rtp_trace.txt` (port of the macOS RTP tab, plus echo canceller statistics) |
| Contacts UI | Organization in the contact editor and in brackets on the contact's first line; taller group bar, weight-550 group names, centred disclosure triangle; list row heights and unread badge follow the system font |
| Theme | Dark theme for contact rows, group bars, secondary text, default avatar, Messages window tiles and session area, file transfer rows, bottom bar icons; live light/dark switching (palette from the desktop portal when the platform theme does not report it); XWayland on GNOME so the window frame follows the theme |
| Main window | Search box no longer focused at start; larger bottom bars with 2 px between call buttons, matching Calls/Sessions bar |
