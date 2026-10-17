# Blink Qt — feature gaps compared with WhatsApp (todo)

Status as of 2026-10-08, from the code in `blink/messagepane/*`, `blink/messages.py` and `ui-plan.md`; updated the same evening after reactions were added.

Already on par: text with reply/quote, emoji reactions, edit, delete for both sides, forward, delivery and read marks, typing indicator, voice notes with waveform, pictures and videos with captions/crop/original, files, camera, screenshot, send current location and request location, search within a conversation, jump to date, media grid with multi-select, message info, OpenPGP end-to-end encryption.

## Conversations

- [ ] Group chats in the message pane (design still open across SylkServer, Sylk Mobile and Blink Qt)
- [x] Reactions, as on Sylk Mobile: a reply whose body is the emoji. The message menu starts with ❤️ 👍 😂 😮 😢 🙏 and a + that opens the emoji picker on hover (quick reactions + mobile's six categories). Not on location shares, notes, uploads or failed messages. Pure-emoji replies stay out of the last-message line. (`blink/messagepane/reactions.py`)
- [x] Colour emoji: emoji in bubbles drawn from the colour emoji font (not the text font's outline symbols); `fonts-noto-color-emoji` recommended
- [ ] Emoji picker in the composer (the reaction picker can be reused); emoji typed in the composer still use the text font
- [ ] Reaction badges on the original bubble (WhatsApp style): not done; mobile shows reactions as replies too, so it would need both clients
- [ ] Starred messages
- [ ] Pinned conversations
- [ ] Muted conversations
- [ ] Archived conversations
- [ ] Search across all conversations (`model.search()` covers only the open one)
- [ ] @mentions (after group chats)

## Media and content

- [ ] Start a live location share and a meet-up (incoming ones already display)
- [ ] Link previews (OpenGraph)
- [ ] Sticker picker
- [ ] GIF picker
- [ ] Share a contact card (vCard)
- [ ] Polls
- [ ] View-once media
- [ ] Video trim in the attachment preview (needs an H.264 encoder in the dependencies)

## Privacy and lifecycle

- [ ] Disappearing messages (per-conversation timer)
- [ ] Block a contact from the conversation (the `blocked` group kind exists, there's no action in the pane)
- [ ] Export a conversation (the SylkServer journal partly covers backup)

## Platform

- [ ] Per-conversation notification settings
- [ ] Status / Stories, Channels, Communities, broadcast lists (probably out of scope)
- Linking devices by QR code: not needed, because each device registers with the SIP account itself

## Plumbing left from Phase B

- [ ] Move unread counting and display receipts out of the chat window into the pane
- [ ] SylkServer: add `X-Sylk-User-Agent` to messages written after an HTTP upload (`web.py`)

## Suggested order

1. Group chats
2. ~~Reactions with an emoji picker~~ (done)
3. Starting a live location share and a meet-up
4. Link previews
5. Emoji picker in the composer
