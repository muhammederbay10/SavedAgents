# 0011: How the bot sends DM replies

- Status: proposed with SAV-12. It takes effect when SAV-12's pull request is merged. The points under [For Muhammed](#for-muhammed) are open questions, not approved exceptions.
- Revised after review round 1 (18 Nov 2026): 503 is no longer retried, the window is checked before every request and capped at 24 hours, and languages without built-in wording need wording from the caller.
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

1. **The caller passes the time of the user's last message**, as Meta reported it (`received_at` of their latest `ChatMessage` or `CaptureEvent` from SAV-11). Storing that time is the caller's job: the client keeps no state.
   - The client checks it against its clock **before every request**, retries included, so a wait between attempts can't carry a message past the window. When the window has closed, it stops and returns `OUTSIDE_REPLY_WINDOW` with the number of requests already made, and logs `dm_skipped_outside_reply_window`. Meta's own "outside the window" answer gives the same result. Skipping is a result, not an error.
   - `INSTAGRAM_REPLY_WINDOW_HOURS` may shorten the window but never widen it: values above 24, zero or less, infinity and NaN are refused at load, and the client also caps it at 24 hours.
2. **Only failures that show the message was not taken are retried:** Meta's throttling codes above or HTTP 429 (the call was refused), and a connection that never opened (nothing was sent). A timeout after the request went out, or any 5xx including 503, may have delivered the message, so it raises `MessengerDeliveryUnknownError` without a retry. A bare 503 shows nothing about delivery, and Meta documents no guarantee for it (a non-idempotent POST must not be retried blindly, RFC 9110 §9.2.2). A duplicate is still possible if Meta reports throttling for a message it did take.
3. **Backoff:** the wait starts at `SEND_API_RETRY_BASE_DELAY_SECONDS`, doubles each time and never passes `SEND_API_RETRY_MAX_DELAY_SECONDS`, also when Meta sends a longer `Retry-After`. After `SEND_API_MAX_ATTEMPTS` the error is raised. A chat reply that waits longer than a few seconds is no longer useful, so the caller decides what to do then (e.g. drop it, or send the error message later).
4. **Every other failure is a typed error** (`MessengerRequestError` for a refused token, a blocked user or a bad body; `MessengerResponseError` for 200 without a message id). The client never swallows one.
5. **Separate settings.** `MessengerSettings` holds the Send API settings, so `/health` and the migrations don't need the bot's token (the same split as SAV-10's `ModelSettings`).
6. **Any language, through caller-supplied wording.** Every template takes a `ReplyWording`: the bot's own words in one language, including the error text, the "which one?" question and its "None of these" button, and the list separator. `reply_wording(language, localized)` returns:
   - the `localized` wording when the caller supplies it, for any language;
   - otherwise built-in wording, which exists for English and Turkish;
   - otherwise it raises `ReplyWordingUnavailableError`. A user is never silently answered in another language (record 0005 §9, SAV-22).

   Supplied wording is validated: every field is non-empty and has exactly its placeholders. Names, open categories, genres, areas, platforms, option labels and count labels are shown exactly as the caller passes them (record 0005).
7. **"Which one?" buttons carry their number** ("1. Kronotrop Bebek"), so two options stay apart when Meta cuts a long title.
8. **Logs hold no personal data:** no recipient id, message id, text, token or Meta error message. They hold the kind of message, the attempt, the HTTP status and Meta's numeric error code.

## For Muhammed

| # | Choice made here | Why, and what the other option is |
|---|---|---|
| Q1 | A failure that may have delivered the message (a timeout after sending, any 5xx) is not retried, so that message can be lost | Retrying could send the user the same DM twice. Retrying more widely would need his decision |
| Q2 | Wording for languages other than English and Turkish comes from the caller | How SAV-17 produces it is SAV-17's design; this ticket adds no model call or prompt. No language exception is assumed: without wording, the call fails explicitly |
| Q3 | `.env.example` uses `https://graph.facebook.com/v27.0` | Record 0004's test used `graph.facebook.com` with the bot's Unified-login token. A token from "Instagram API with Instagram Login" would need `graph.instagram.com` |
| Q4 | Quick replies were not tested live | Record 0004 tested text and `reply_to` only. Meta says quick replies are not shown on desktop |

## Alternatives considered

- **Raise an error for a closed window.** The ticket says "skip and log"; a result keeps the caller's normal path simple and still lets it tell the two apart.
- **Fall back to English for other languages.** That was the first version; review found it breaks record 0005 §9, because the bot's own words would be English even when names stay in the user's language.
- **Retry HTTP 503.** Also in the first version. A server that took the message and then answered 503 would deliver it twice.
- **Look up the user's last message inside the client.** It would tie the Send API client to the database and to SAV-11's tables. Passing the time keeps it a pure HTTP client.
- **Share SAV-10's OpenRouter transport.** The retry rules differ: a model call can be repeated safely, a DM can't.

## Consequences

- **SAV-11:** ingestion must keep the time of each user's last message where the jobs can read it (it already passes `received_at` in the job payloads). Taps on a quick reply arrive as `message.quick_reply.payload` in the webhook; the adapter has to read it for `resolve_choice`.
- **SAV-17 (response agent):** sends the intro and one `result_reply` per result, caps the list, and handles `OUTSIDE_REPLY_WINDOW` and the errors. For a user whose language has no built-in wording (`has_built_in_wording` is false), it supplies a `ReplyWording` in that language; without one, the templates can't be used for that user.
- **SAV-14 (pipeline):** sends confirmations and "which one?" questions before the user ever asks anything, so it needs the same wording for such users.
- **New settings:** `INSTAGRAM_GRAPH_API_URL`, `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN` (a secret), `INSTAGRAM_REPLY_WINDOW_HOURS`, `SEND_API_TIMEOUT_SECONDS`, `SEND_API_MAX_ATTEMPTS`, `SEND_API_RETRY_BASE_DELAY_SECONDS`, `SEND_API_RETRY_MAX_DELAY_SECONDS`.
- **Dependencies:** `httpx` moves from the dev extras to the runtime dependencies. It isn't new to the project; SAV-10 makes the same change.
