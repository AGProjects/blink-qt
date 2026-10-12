# Data manager: File Transfers

The Data manager has two sections: **Messages** (bulk management of the
message history) and **File Transfers**. File Transfers finds the file
transfer messages whose file is still on the server but is not on this
computer, lets the user narrow them down the same way as messages, and
downloads the whole selection with one button.

Typical use: a new computer that imported the journal (thousands of transfer
messages, no files), or a conversation whose pictures were never scrolled
into view and so never fetched automatically.

Where it is:

| Client | Menu | Section switch |
|---|---|---|
| Blink for macOS | Blink > Data Manager... | segmented control at the top of the window: Local Messages / File transfers on server / Import from device |
| Blink Qt | Blink > Accounts > Data manager | tabs: Local Messages / File transfers on server / Import from device / Export to device |

## What is listed

Every live (not deleted) row of the account whose content type is
`application/sylk-file-transfer` or `application/vnd.gsma.rcs-ft-http+xml`
is examined. Its envelope is parsed (`file_transfer_envelope`) and the row
falls into exactly one bucket:

| Bucket | Rule | Listed? |
|---|---|---|
| here | the file is on this computer (see below) | no, counted |
| unreadable | no envelope (an encrypted body nobody decrypted), no filename, or no message id | no, counted |
| never uploaded | the envelope has no `url` | no, counted |
| expired | `until` is in the past | no, counted |
| gone | the server answered 404/410 before | no, counted |
| **Failed before** | an earlier fetch failed for another reason (403, a key that does not open it, ...) | yes |
| **Encrypted, no key here** | `.asc` and no private key on this device | yes, not downloaded |
| **On the server** | anything else | yes |

The counted buckets are shown under the headline ("812 of 1430 file
transfers are here, 40 expired on the server, 3 gone from the server, ...")
so the numbers always add up to the account's transfers.

### When a file counts as here

The same rule the message pane uses to decide whether a bubble has its file,
applied to the whole history at once: the storage folder is listed once
(`scan_transfer_storage`) instead of checking file by file.

* **Blink for macOS** (`FileTransferCache.local_file`):
  `file_transfers/<account>/<peer>/<transfer id, else filename>/<name without .asc>`,
  non-empty, under any account folder for the same peer. The account and
  peer are the row's `local_uri` and `remote_uri` as stored.
* **Blink Qt** (`blink.messagepane.files.local_file`):
  `file_transfers/<account>/<peer>/<id>/<name>` or the older
  `downloads/<id>/<name>`, under any account and peer folder, where `<id>`
  is any of the envelope's `transfer_id`, the id segment of the URL, or the
  message id. For an `.asc` only the decrypted file counts; partial
  downloads (`.download`) do not.

### Where a previous failure comes from

* **Blink for macOS**: the `error` the conversation writes into the stored
  envelope on a permanent failure (`transfer_error_note`), and the failures
  `FileTransferCache` knows of in this run. An error containing `HTTP 404`
  or `HTTP 410` means gone.
* **Blink Qt**: `.failure.json` in the transfer's folder; `kind: gone`
  means gone.

## Filters

Account, **Type** (Images, Videos, Audio, Other files; the row's category,
else the MIME type, else the extension), **Status** (the three listed
buckets), **Contact**, and **Period** (year, then month, then day). As in
Messages, each filter lists what is left once every other filter is
applied, so they narrow each other in any order; Reset Filters clears them.
The headline shows how many files the selection has, how many of them
Download would fetch, and their total size. Clicking a row shows the
transfer in full: name, size, type, status and reason, contact, time,
message id, transfer id, URL, expiry, and where the file is looked for.

## Download

Download takes the **whole filtered selection** (as Delete does in Messages,
never rows picked in the list), keeps the On the server and Failed before
rows, one per message id, and asks for confirmation with the count and the
total size. Files go newest first, through the same path the message pane
uses, so they end up exactly where a click on the bubble would put them:

* **Blink for macOS**: `FileTransferCache.fetch(..., force=True)`, three
  files at a time. `.asc` files are decrypted with the private key they
  were sealed to among the keys on this device. A permanent failure is
  written into the envelope as the conversation does (a 404 there makes the
  conversation drop the message the next time it is shown); a success
  clears an old stored error.
* **Blink Qt**: `SessionManager.get_file_from_url` on the file-io thread,
  one file at a time, with `force` for Failed before rows. A file with no
  progress for 120 seconds is given up and the next one starts.

The user's click is the consent: the contact's `auto_download_media` (which
only governs automatic fetching) does not apply.

While it runs, a progress bar (files done plus the fraction of the files
being fetched) and the names of the files being fetched are shown. Stop
lets the files being fetched finish (Qt asks the current one to stop) and
starts no new one. At the end: "Downloaded X of N files", the first three
failures with their reason (the rest in the log), and the list is read again,
so what was fetched disappears from it.

An open conversation that shows one of the fetched files picks it up at
once: macOS through `SMSWindowManager.transferFetchedInDataManager`, Qt
because the pane's fetcher now reacts to any finished download, not only to
its own.

Not offered: Bonjour accounts (their files do not go through the server) and
disabled accounts.

## Code

| | Blink for macOS | Blink Qt |
|---|---|---|
| classification (no GUI, testable alone) | `DataManage.py`: `scan_transfer_storage`, `build_file_rows`, `FileRow` | `blink/data_manage.py`: same names |
| queries | `HistoryManager.py` `ChatHistory.data_file_transfer_accounts`, `data_file_transfers` | `blink/history.py` `MessageHistory.data_file_transfer_accounts`, `data_file_transfers` |
| panel | `DataManagerWindow.py` `FileTransfersPanel` | `blink/datawindow.py` `FileTransfersPanel` |
| open conversation refresh | `SMSWindowManager.transferFetchedInDataManager` | `blink/messagepane/fetch.py` `AutoFetcher._NH_BlinkFileTransferDidEnd` |

`FileRow` keeps the field names `type` and `action` (the status) so the
Messages helpers `select`, `count_by` and `aggregate_days` drill into it
unchanged.

## Testing

1. An account with history from another device and few files here: open the
   section; the counts under the headline add up to the transfers of the
   account; the activity log has one `[data] ... file transfers of ... are
   on the server and not here` line with the same numbers.
2. Choose a contact and a month; Download; confirm. Every file arrives in
   the folder the conversation uses; opening the conversation shows the
   pictures without fetching them again.
3. With the conversation already open, download its files from the Data
   manager: the bubbles fill in without reopening the conversation.
4. Stop during a long download: the file being fetched finishes (macOS) or
   stops (Qt), no new one starts, the result says "stopped", and a second
   Download continues with what is left.
5. A transfer whose file was removed from the server: it fails with HTTP
   404, and the next Refresh counts it as gone instead of listing it.
6. Encrypted transfers (`.asc`): with the private key present they are
   downloaded and decrypted; without it they are listed as "Encrypted, no
   key here" and Download skips them.
7. Refresh after the download: what was fetched is no longer listed, and
   "here" has grown by the number downloaded.
