# OpenRouter response fixtures

These files are **not recordings**. They were written by hand from the response
shapes in OpenRouter's public documentation as read on 17 and 18 Nov 2026
(Decisions API, speech-to-text, chat completions, router metadata, embeddings,
errors), because the live checks in SAV-7 did not keep their raw responses.

The chat completion files carry `openrouter_metadata`, the routing metadata that
OpenRouter adds when a request sends `X-OpenRouter-Metadata: enabled`. The shape
follows its "Router metadata" guide; that guide's own examples are Responses API
replies, so the placement next to `choices` is the guide's rule ("alongside the
rest of the response payload"), not a copied example.

Every value in them is invented: no caption, transcript, message, media, key
or user id from a real account. Ids such as `gen-synthetic-0001` are made up.

When a live smoke test (see the README, "Live smoke tests") shows that a real
response differs from one of these files, update the file and the client
together.

| File | Endpoint | What it shows |
|---|---|---|
| `decisions_response.json` | `POST /api/alpha/decisions` | One `noul`, one `choice` and one `score` answer, and a `refusal` |
| `chat_completion_schema.json` | `POST /api/v1/chat/completions` | A JSON-schema reply: the JSON is a string in `content`; routing metadata names the one selected provider |
| `chat_completion_tool_calls.json` | `POST /api/v1/chat/completions` | A reply that calls a tool; `content` is null and `arguments` is a JSON string |
| `chat_completion_after_fallback.json` | `POST /api/v1/chat/completions` | A reply served by the pinned provider after a failed attempt at another one |
| `chat_completion_cache_replay.json` | `POST /api/v1/chat/completions` | A cache replay: OpenRouter strips the routing metadata from it |
| `error_no_allowed_providers.json` | any completion route | The HTTP 404 when no allowed endpoint can serve the request, with its metadata |
| `transcription_verbose.json` | `POST /api/v1/audio/transcriptions` | `verbose_json`: text, language, duration, segments, words |
| `transcription_plain.json` | `POST /api/v1/audio/transcriptions` | `json`: text and usage only |
| `embeddings_response.json` | `POST /api/v1/embeddings` | Two 4-dimension vectors, returned out of order |
| `error_rate_limited.json` | any | The error body of an HTTP 429 |
