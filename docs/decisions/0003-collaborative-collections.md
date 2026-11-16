# 0003: Saving to a collaborative collection does reach the bot

- Status: spike result recorded. Whether to use this as a capture path is **Muhammed's decision** (see [Proposal](#proposal)).
- Date: 2026-11-16
- Ticket: SAV-6 (the ticket asked for `0002-…`; that number went to the SAV-5 payload record, so this is `0003`)

## Context

`docs/ARCHITECTURE.md` §7 asked: if a user saves a post into a collaborative collection that the bot account belongs to, does the bot get a webhook? If yes, users could keep tapping Save as they do today, with no sharing step. The setup from SAV-5 was reused: development mode, the recorder behind ngrok, and Muhammed's personal account (an Instagram Tester).

Muhammed created a **new** collaborative collection with the chat `savedagent2026` as collaborator; collaborators can only be added when a collection is created. He then saved a reel, a photo post, and a reel with a caption into it.

## Result: yes

Every save arrived as an ordinary `messages` webhook in the 1:1 chat between the user and the bot, from the user's `sender.id`, with a valid `X-Hub-Signature-256`. Fixtures: `tests/fixtures/webhooks/instagram_collection_save_*.json`.

| Saved | Attachment type | What the payload holds |
|---|---|---|
| Two reels, saved 20 s apart | `unsupported_type` | **Only a `url`**: a signed `lookaside.fbsbx.com` link that returned the **MP4 file** (about 7 MB each) |
| Photo post | `ig_post` | `ig_post_media_id`, the caption in `title` (166 characters), and a JPEG link. The same shape as a DM share |
| Reel with a caption | `unsupported_type` | Only the MP4 link (2.5 MB). **The caption was not sent** |

### Compared with sharing by DM (record 0002)

| | DM share | Save to the shared collection |
|---|---|---|
| Reel caption | ✅ full caption | ❌ none |
| Reel video file | ❌ only the public page link | ✅ direct MP4 link |
| Reel id or page link | ✅ `reel_video_id` + page URL | ❌ none |
| Photo post | `ig_post` with caption and image | the same |
| Which collection | the user can type a hint | ❌ not in the payload; every collection shared with the bot posts into the same chat |
| Effort for the user | Share → pick the bot | Tap Save (after a one-time setup) |

### Why it works
Muhammed confirmed that **every save into the collaborative collection is automatically posted into his DM chat with the bot**. A collaborative collection is tied to that chat, so each save becomes a message, and the message triggers the webhook. The two reel events were two saves.

### The API can't add the missing caption
With Muhammed's approval, we made read-only Graph API calls (`graph.facebook.com`) with the bot's Unified-login token (`EAA…`) to see whether the API returns more than the webhook:
- Reading a message directly by the webhook's `mid` failed twice with `500 "An unknown error has occurred"`.
- Listing the bot's conversations (`/{ig-user-id}/conversations?platform=instagram` with `messages{id,created_time,is_unsupported,attachments,shares}`) worked. It marked **every shared post as `is_unsupported: true` and returned no attachments and no shares for them**: the collection saves and the DM shares of reels, photos and carousels alike. Only plain texts (`is_unsupported: false`) and one directly sent photo (with `image_data`) came back normally.

So the **webhook is the richest source**: the API gives less, not more. A reel saved to the collection has to be understood from its video (and audio) alone.

## Open points
1. **Did the bot have to accept anything** (an invite to the collection)? To confirm.
2. **`unsupported_type` is undocumented.** Meta sends it but doesn't describe it, so its shape can change or disappear without notice.
3. **Link lifetime:** the MP4 and JPEG links worked minutes after the save. Their lifetime is unknown; download right away (as in 0002).
4. **New users:** not tested with an account that never chatted with the bot, nor outside development mode.

## Proposal

Use this as a **second, optional capture path** next to the DM share, not as a replacement. Muhammed decides.

- **Why:** it's the zero-effort path the design hoped for. Better still, it delivers the reel's **video file**, which DM shares don't (0002, consequence 1). It's an official webhook and an official CDN link, so it is not scraping.
- **What it can't do:** no caption, no link back to the original reel, no collection name, and the Graph API doesn't fill these in either (see above). A reel saved this way has to be understood from the video alone (Gemini Decision 1 + speech-to-text), and the confirmation DM can't link to the original post. When the bot isn't confident, it asks the user in the chat, as §1 "Ask when unsure" already says.
- **How it could work:** the adapter (SAV-11) maps `unsupported_type` with an MP4 link to a `CaptureEvent` with `media_type="reel"`, `media_url` = the MP4, no caption and no `post_url`. The pipeline downloads the video at once. Users are told: "Create a collection, switch on Collaborative, add @savedagent2026. Everything you save there is indexed." One shared collection per user is enough, because the collection name doesn't arrive anyway.
- **A combination worth testing later:** for a reel, a DM share gives the caption and page link, and the collection gives the video. If a user does both, the two could be matched on timing, but there's no shared id, so this is best-effort only.
- **Before relying on it:** re-test open points 1 and 4 with a second tester. Put it behind a setting, so it can be switched off if Meta changes `unsupported_type`. Keep the DM share as the documented main path for App Review.
