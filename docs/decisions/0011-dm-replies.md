# 0011: How the bot sends DM replies

- Status: proposed with SAV-12. It takes effect when SAV-12's pull request is merged. The points under [For Muhammed](#for-muhammed) are choices made inside the ticket's scope that he may want to change.
- Date: 2026-11-18
- Ticket: SAV-12 (0009 and 0010 are taken by SAV-11 and SAV-10, which are not merged yet)

## Context

`docs/ARCHITECTURE.md` §3.1 and §3.5 and record 0004 say what the bot sends: text, "which one?" questions, and one reply per search result that quotes the saved reel (`reply_to.mid`). Instagram only accepts a reply within 24 hours of the user's last message, and the Send API has rate limits. SAV-25's review added that the ~5-result cap is for readability, not a rate-limit guarantee, so the client must handle rate limits itself.

Meta's documentation, read on 18 Nov 2026 (no live call was made for SAV-12):
- the body is `{"recipient": {"id": IGSID}, "message": {"text": …}}`; `reply_to: {"mid": …}` sits next to `message`, and quick replies go in `message.quick_replies` as `content_type: "text"`, `title`, `payload`;
- at most 13 quick replies, titles cut after 20 characters, text at most 1000 bytes;
- throttling is reported with error codes 4, 17, 32, 613, 80002 and 80006; the HTTP status is not documented;
- "outside of allowed window" is code 10 with subcode 2018278 or 2534022;
- the current Graph API version is v27.0.

## Decision

1. **The caller passes the time of the user's last message**, as Meta reported it (`received_at` of their latest `ChatMessage` or `CaptureEvent` from SAV-11). The client compares it with its clock and skips the message when it is older than `INSTAGRAM_REPLY_WINDOW_HOURS`. Skipping is a result (`OUTSIDE_REPLY_WINDOW`), not an error, and is logged as `dm_skipped_outside_reply_window`. Meta's own "outside the window" answer gives the same result. Storing that time is the caller's job: the client keeps no state.
2. **A retry never sends a DM twice.** Only failures where Meta certainly didn't take the message are retried: the throttling codes above or HTTP 429, a connection that never opened, and HTTP 503. A timeout after the request went out, or another 5xx, raises `MessengerDeliveryUnknownError` without a retry.
3. **Backoff:** the wait starts at `SEND_API_RETRY_BASE_DELAY_SECONDS`, doubles each time and never passes `SEND_API_RETRY_MAX_DELAY_SECONDS`, also when Meta sends a longer `Retry-After`. After `SEND_API_MAX_ATTEMPTS` the error is raised. A chat reply that waits longer than a few seconds is no longer useful, so the caller decides what to do then (e.g. drop it, or send the error message later).
4. **Every other failure is a typed error** (`MessengerRequestError` for a refused token, a blocked user or a bad body; `MessengerResponseError` for 200 without a message id). The client never swallows one.
5. **Separate settings.** `MessengerSettings` holds the Send API settings, so `/health` and the migrations don't need the bot's token (the same split as SAV-10's `ModelSettings`).
6. **Fixed wording is data, values pass through.** The five fixed messages have English and Turkish wording in one table. A language without wording gets English. Names, open categories, genres, areas and platforms are shown exactly as the caller passes them (record 0005).
7. **"Which one?" buttons carry their number** ("1. Kronotrop Bebek"), so two options stay apart when Meta cuts a long title.
8. **Logs hold no personal data:** no recipient id, message id, text, token or Meta error message. They hold the kind of message, the attempt, the HTTP status and Meta's numeric error code.

## For Muhammed

| # | Choice made here | Why, and what the other option is |
|---|---|---|
| Q1 | A failure after the request went out is not retried | A retry could send the user the same DM twice. The other option is to retry and accept a rare duplicate |
| Q2 | Languages other than English and Turkish get the English wording | Fixed templates need a translation per language. The other option is to let the response agent (SAV-17) phrase these messages in any language, at the cost of a model call per message |
| Q3 | `.env.example` uses `https://graph.facebook.com/v27.0` | Record 0004's test used `graph.facebook.com` with the bot's Unified-login token. A token from "Instagram API with Instagram Login" would need `graph.instagram.com` |
| Q4 | Quick replies were not tested live | Record 0004 tested text and `reply_to` only. Meta says quick replies are not shown on desktop |

## Alternatives considered

- **Raise an error for a closed window.** The ticket says "skip and log"; a result keeps the caller's normal path simple and still lets it tell the two apart.
- **Look up the user's last message inside the client.** It would tie the Send API client to the database and to SAV-11's tables. Passing the time keeps it a pure HTTP client.
- **Share SAV-10's OpenRouter transport.** The retry rules differ: a model call can be repeated safely, a DM can't.

## Consequences

- **SAV-11:** ingestion must keep the time of each user's last message where the jobs can read it (it already passes `received_at` in the job payloads). Taps on a quick reply arrive as `message.quick_reply.payload` in the webhook; the adapter has to read it for `resolve_choice`.
- **SAV-14 (pipeline):** sends the save confirmation and the "which one?" question.
- **SAV-17 (response agent):** sends the intro and one `result_reply` per result, caps the list, and handles `OUTSIDE_REPLY_WINDOW` and the errors.
- **New settings:** `INSTAGRAM_GRAPH_API_URL`, `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN` (a secret), `INSTAGRAM_REPLY_WINDOW_HOURS`, `SEND_API_TIMEOUT_SECONDS`, `SEND_API_MAX_ATTEMPTS`, `SEND_API_RETRY_BASE_DELAY_SECONDS`, `SEND_API_RETRY_MAX_DELAY_SECONDS`.
- **Dependencies:** `httpx` moves from the dev extras to the runtime dependencies. It isn't new to the project; SAV-10 makes the same change.
