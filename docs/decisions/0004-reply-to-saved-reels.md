# 0004: Answers send the saved reels back by replying to them

- Status: accepted by Muhammed in chat on 2026-11-16.
- Date: 2026-11-16
- Ticket: SAV-25

## Context

When a user asks "restaurants in Beşiktaş from my Food collection", a list of names and Maps links isn't enough. Muhammed wants the bot to **send the saved reels back**, so the user sees the videos again. Two facts made this hard:
- reels saved into a collaborative collection arrive without any link to the original post (record 0003);
- the Graph API calls we tried didn't return one either (record 0003).

But every saved item did arrive as a **message in the user's chat with the bot**, and the webhook gives that message's id (`mid`).

## Test

With Muhammed's approval, the bot sent three messages to his account through the Send API (`POST https://graph.facebook.com/{ig-user-id}/messages`, Unified-login token):

| Sent | Body | What Instagram showed |
|---|---|---|
| A reply to a reel saved into the collaborative collection | `recipient.id`, `message.text`, `reply_to.mid` = the save's `mid` | "Replied to you", with **the saved reel quoted** above the text |
| A reply to a reel shared by DM | the same, with the DM's `mid` | the same: the reel quoted |
| A text containing the DM reel's `https://www.instagram.com/reel/…` link | `message.text` only | a **rich preview card** with the reel's image and the creator's name |

All three returned HTTP 200 with a `message_id`.

## Decision

1. **Every capture keeps its message id.** `CaptureEvent.source_message_id` and `SAVED_ITEM.source_message_id` store the `mid` of the DM or collection save.
2. **Answers reply to the saved reels.** For each result, the bot sends one message with `reply_to.mid` set to the item's `source_message_id`, plus a short line such as "🍽 1/3 · Kronotrop, Beşiktaş · open now · Maps link". A short intro message comes first.
3. **Fallbacks, in order:** the post link (a preview card, for DM shares and imports that have one), then the name with a Maps or TMDB link.
4. **Limits:** at most about 5 results per answer, with "show more" for the rest. This keeps the chat readable and stays well within the Send API's rate limits.

## Alternatives considered

- **Names and links only** (the earlier design): this doesn't show the reels again, and collection saves have no link.
- **The post link as a preview card:** this looks good, but it only works when we have a link, which collection saves don't.
- **Re-sending our stored copy of the video:** this loses the creator and the original post, it's weaker on copyright, and it needs us to store and upload the file. We keep the stored copy for analysis (deep look) only.

## Consequences

- **SAV-9 (schema):** add `source_message_id` (text, nullable, indexed per user) to `saved_item`, and delete it with "delete my data".
- **SAV-11 (ingestion):** fill `source_message_id` from `message.mid` for every share and collection save.
- **SAV-12 (Send API client):** support `reply_to.mid` and the "result" message template, with tests based on the request body above.
- **SAV-17 (response agent):** answers that list items are sent as an intro plus one reply per result, using the fallbacks above. Tests cover the order and the cap.
- **Open:** whether replies still quote the reel for very old messages, or after the user deletes the original message from the chat, is untested. When the reply fails or the quote is missing, the fallbacks apply.
