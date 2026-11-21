# Send API response fixtures

Answers of the Instagram Send API (`POST /{ig-user-id}/messages`) for the messenger tests (SAV-12).

All of them are **synthetic**: no live DM was sent for SAV-12, and the bodies Meta returned in the
SAV-25 test (decision record 0004) were not kept. They follow the shapes Meta documents, so the
file names start with `synthetic_`, like `tests/fixtures/webhooks/synthetic_instagram_share_legacy.json`.

| File | What it stands for | Source of the shape |
|---|---|---|
| `synthetic_send_success.json` | A sent message: `recipient_id` and `message_id` | Instagram "Messaging API" page; record 0004 ("HTTP 200 with a `message_id`") |
| `synthetic_error_rate_limited.json` | Throttling, code `613` / subcode `2534040` | Messenger Platform "Error codes" page; Graph API "Rate limiting" page |
| `synthetic_error_outside_window.json` | The 24-hour window has closed, code `10` / subcode `2018278` | Messenger Platform "Error codes" page |
| `synthetic_error_invalid_token.json` | A refused access token, code `190` | Graph API "Handling errors" page |
| `synthetic_error_invalid_parameter.json` | A refused parameter, code `100`; the answer tests (SAV-17) use it for a `reply_to.mid` that points to an old or deleted message. Meta does not document which error such a reply gets | Graph API "Handling errors" page |

Ids are made up. Meta's HTTP status for these errors is not documented; the tests send them with
status 400 and, for throttling, also with 429.
