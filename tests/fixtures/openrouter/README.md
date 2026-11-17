# OpenRouter response fixtures

These files are **not recordings**. They were written by hand from the response
shapes in OpenRouter's public documentation as read on 17 Nov 2026 (Decisions
API, speech-to-text, chat completions, embeddings, errors), because the live
checks in SAV-7 did not keep their raw responses.

Every value in them is invented: no caption, transcript, message, media, key
or user id from a real account. Ids such as `gen-synthetic-0001` are made up.

When a live smoke test (see the README, "Live smoke tests") shows that a real
response differs from one of these files, update the file and the client
together.

| File | Endpoint | What it shows |
|---|---|---|
| `decisions_response.json` | `POST /api/alpha/decisions` | One `noul`, one `choice` and one `score` answer, and a `refusal` |
| `chat_completion_schema.json` | `POST /api/v1/chat/completions` | A JSON-schema reply: the JSON is a string in `content` |
| `chat_completion_tool_calls.json` | `POST /api/v1/chat/completions` | A reply that calls a tool; `arguments` is a JSON string |
| `transcription_verbose.json` | `POST /api/v1/audio/transcriptions` | `verbose_json`: text, language, duration, segments, words |
| `transcription_plain.json` | `POST /api/v1/audio/transcriptions` | `json`: text and usage only |
| `embeddings_response.json` | `POST /api/v1/embeddings` | Two 4-dimension vectors, returned out of order |
| `error_rate_limited.json` | any | The error body of an HTTP 429 |
