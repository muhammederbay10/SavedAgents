# 0010: Model clients, and the provider that serves DeepSeek

- Status: proposed. Three points need Muhammed's answer before the pull request: see [Needs the owner's decision](#needs-the-owners-decision). The model choices themselves were approved in SAV-7 (decision record 0008).
- Date: 2026-11-17
- Ticket: SAV-10 (0008 is SAV-7's record on the model choices; 0009 is SAV-11's on webhook ingestion, which arrives with its own pull request)

## Context

Every model call goes through OpenRouter with one key, and every model sits behind a small interface so it can be swapped in config (`docs/ARCHITECTURE.md` §3.9, record 0008). SAV-10 builds those interfaces and their clients. Record 0008 left three things to this ticket:

1. **Which provider serves `deepseek/deepseek-v4.1-flash`.** Muhammed approved pinning it to a provider hosted outside China, as a setting, with no fallback to an unapproved host and a clear error when no route is available. The serving region must be backed by evidence; where a company is registered is not enough.
2. **How the speech-to-text result tells an empty transcript from a failure**, so SAV-14 can route on the transcript.
3. **The exact endpoint contracts.** SAV-7's live checks did not keep their scripts or raw responses.

## Decision

### The clients

1. **Four interfaces in `app/models/base.py`:** `Classifier.decide`, `Transcriber.transcribe`, `Generator.generate` and `Embedder.embed`, with plain value types. The rest of the code imports only `base`, `errors` and `factory` from `app/models/`; a test (`tests/test_model_boundary.py`) fails on any other import from the package, and on any model SDK import outside it.
2. **One HTTP transport, no model SDK.** `app/models/openrouter.py` adds the key, applies each call's timeout, retries, and maps failures to error types. The clients build and read JSON themselves. An SDK would not cover the decisions endpoint or OpenRouter's JSON transcription format, and would be a second new dependency.
3. **Retries:** timeouts, connection failures and HTTP 408, 429, 500, 502, 503, 504, 524 and 529 are tried again, up to `OPENROUTER_MAX_ATTEMPTS` in total. The wait starts at `OPENROUTER_RETRY_BASE_DELAY_SECONDS`, doubles each time, follows a longer `Retry-After` header, and never passes `OPENROUTER_RETRY_MAX_DELAY_SECONDS`. Every other failure is raised at once. An `error` object inside an HTTP 200 is treated as the error it describes.
4. **Errors and logs carry no content.** An error names the operation, the model, the HTTP status and the provider's short error label (for example `rate_limit_exceeded`). It never quotes a request, a response or the provider's own message, because a provider can quote its input back. Logs hold the same fields plus the attempt number and the elapsed time. Captions, transcripts, messages and media are also left out of the `repr` of every value type.
5. **Model settings are their own class, `ModelSettings`,** read with `get_model_settings()`. The health endpoint and the migrations call no model, so they don't need an API key to start. Like `Settings`, no field has a default. Both endpoint URLs must be `https`, because the key travels in a header.
6. **Decisions use their own endpoint and contract** (`OPENROUTER_DECISIONS_URL`, not chat completions). A question the model refused, left out or answered unusably comes back as `Unanswered` for that question only; the other answers of the call stay valid.
7. **Embeddings:** the client sends `dimensions=EMBEDDING_DIMENSION` and raises `EmbeddingDimensionError` if any returned vector has another size.

### Speech-to-text: an empty transcript is a result, a failure is an error

`FallbackTranscriber` asks the default model, then the fallback model when the default fails or returns no words. The transcript's `attempts` lists every model that was asked, with `text`, `empty` or `error`.

| Default model | Fallback model | What `transcribe` does |
|---|---|---|
| words | not asked | returns the default's transcript, with its language and timestamps |
| error or no words | words | returns the fallback's transcript |
| no words | no words | returns an **empty transcript** (both attempts `empty`) |
| no words | error | returns the default's **empty transcript**; the second attempt is `error` |
| error | error | raises **`TranscriptionFailedError`** with both attempts |

- Language, duration, word timestamps and segment timestamps are `None` when the answering model did not return them. That is the case for the fallback model. `None` means "not available"; nothing is filled in.
- "No words, then an error" returns the empty transcript instead of failing. Reels with only music are common, and a fallback outage should not fail every one of them. The failed second attempt is visible in `attempts`, so SAV-14 can still decide to try again.
- What counts as "nearly empty" stays SAV-14's setting. The client only knows "no words at all".

### The provider that serves DeepSeek

**Chosen: `coreweave/fp8`** (`TEXT_MODEL_PROVIDER=coreweave/fp8`, `TEXT_MODEL_PROVIDER_NAME=CoreWeave`). It is a setting; no provider is named in code.

**Evidence, read on 17 Nov 2026:**

1. OpenRouter's provider list (`GET https://openrouter.ai/api/v1/providers`) gives CoreWeave `headquarters: "US"` and `datacenters: ["US"]`.
2. CoreWeave's own page "All Availability Zones" (`docs.coreweave.com/platform/regions/all-availability-zones`) lists 60 zones. All are in the United States, Canada, Sweden, Norway, Denmark, Spain or the United Kingdom. None is in mainland China, Hong Kong, Macau or anywhere else in Asia.
3. CoreWeave's Serverless Inference model list includes DeepSeek V4.1 Flash.
4. OpenRouter's endpoint list for the model (`GET /api/v1/models/deepseek/deepseek-v4.1-flash/endpoints`) shows `coreweave/fp8` with `response_format`, `structured_outputs`, `tools` and all four `tool_choice` modes, fp8 quantization, and $0.13 per million input tokens and $0.65 per million output tokens.

**What this does not prove.** No page says which zone serves this endpoint. The claim is the weaker one the rule needs: every location CoreWeave publishes is outside China. It is also **not confirmed by a live call**: no request has been sent through the pin, because live calls need Muhammed's approval (SAV-22 §8). The opt-in smoke test `test_text_model_returns_schema_json_from_the_pinned_provider` checks it.

**Other endpoints that were looked at:**

| Endpoint | Evidence of the serving region | Input / output, $ per M tokens | Why not |
|---|---|---|---|
| `fireworks/us` | Fireworks' docs: its US-only Serverless "serves inference exclusively from the US", and lists this model. No page ties OpenRouter's `fireworks/us` to that product | 0.45 / 1.80 | The strongest provider statement, at about 3.5 times the input price. The best alternative if a stated US-only route is wanted |
| `gmicloud/fp8` | OpenRouter lists `datacenters: ["US"]` | 0.18 / 0.72 | No `structured_outputs`; `tool_choice` `required` not supported |
| `venice/fp8` | OpenRouter lists `datacenters: ["US"]` | 0.30 / 1.20 | Dearer; not checked against the provider's own pages |
| `baseten/us`, `inference-net/us`, `wafer/us` | Only the `/us` in the slug. OpenRouter's docs don't say the suffix fixes where a request is processed, and Baseten says residency "depend[s] on the endpoint and agreement" | 0.30 / 1.20, 0.10 / 0.60, 0.05 / 0.57 | A slug is not evidence |
| `deepinfra/fp8` | Headquarters only (US). No statement on where inference runs | 0.14 / 0.42 | The price SAV-7 measured, but no serving-region evidence |
| `together`, `crusoe/fp8`, `parasail/fp8`, others | Headquarters only | 0.29–0.30 / 1.20 | No serving-region evidence |
| `baidu/fp8`, `streamlake/fp8`, `alibaba` | Headquarters in China, or data centres that include China | | Not eligible |

OpenRouter's own US routing (`us.openrouter.ai`) is not an option: this model is not in its in-region list, and it needs a Business or Enterprise plan.

**How the pin is enforced, twice:**

- **In the request.** Every text-model request carries `provider: {"only": [slug], "order": [slug], "allow_fallbacks": false}`. OpenRouter may use that one endpoint and nothing else.
- **In the response.** Every request sends `X-OpenRouter-Metadata: enabled`, so the reply carries `openrouter_metadata` (OpenRouter's "Router metadata" guide, read 18 Nov 2026). `app/models/routing.py` accepts the reply only if all of these hold, and otherwise discards it with `ProviderPinViolationError`:
  - the metadata is there and well formed;
  - exactly one endpoint in `endpoints.available` has `selected: true` (a JSON boolean), and it names `TEXT_MODEL_PROVIDER_NAME` (any letter case) and exactly `TEXT_MODEL`;
  - `attempt`, the number of the attempt that succeeded, is a JSON integer of 1 or more;
  - every entry in `attempts` is the pinned provider and exactly `TEXT_MODEL`. A failed attempt elsewhere still means the request was sent elsewhere;
  - after more than one attempt, `attempts` is complete: one entry per attempt, the last with status 200. If it is missing, empty or shorter, the earlier attempts are unidentified and the reply is refused. With a single attempt the list may be missing (OpenRouter only promises it when the router retried), because the selected endpoint is then the only one called;
  - a top-level `provider` field, if a response has one, names the same provider.
- **Candidates are not calls.** `endpoints.available` lists what the router considered. Another provider listed there with `selected: false` was not sent the request, so it does not fail the reply; the selected endpoint and the attempt history decide.
- **Only documented fields count as proof.** OpenRouter's chat completion reference does not promise a top-level `provider` field, so it is never accepted as evidence. It can only make a reply fail, when it contradicts the metadata.
- **Cache replays are refused.** OpenRouter strips the metadata from them ("Cache hits never include `openrouter_metadata`"), so there is no proof of who served the original. They fail with the same error.
- `DeepSeekGenerator` cannot be built without a pin.

**When no route is available.** If the pinned endpoint cannot serve a call, OpenRouter answers 404 or 503 (its docs name both). A 503 is retried like any outage. After that `generate` raises `ProviderRouteUnavailableError`, which names the provider and says that no fallback is allowed. The request is never sent anywhere else.

**The escalation model** sends the same header but is not pinned. Its result reports the selected provider when the metadata names exactly one, and `None` otherwise; nothing is refused for that.

### Replies with the wrong shape

A chat completion whose choice has no `message` object, whose `content` is neither text nor null, whose `tool_calls` is not a list, or whose tool call is not a function call raises `ModelResponseError`. Null `content` is valid only next to at least one tool call: that is a tool-only reply. An empty string is valid text. A reply is never turned into an empty success.

Embeddings are matched to their texts by `index`, and only a JSON integer from 0 to the number of texts minus one counts. `1.9`, `1.0`, `true`, `"1"` and `-1` are refused, never rounded or converted, and a repeated index is refused.

### Test fixtures

`tests/fixtures/openrouter/` holds responses **written by hand** from OpenRouter's public documentation of 17 and 18 Nov 2026, with invented content. They are not recordings: SAV-7 kept none. The router-metadata guide's own examples are Responses API replies; the fixtures put the same object next to `choices`, as the guide says it is placed. The first approved live smoke run should be used to check them.

## Needs the owner's decision

1. **`httpx` becomes a runtime dependency.** It was already in the project, but only as a development tool (for tests). The clients need an async HTTP client when the app runs, so `pyproject.toml` now lists it under `dependencies`. No new package is installed. AGENTS.md §1 asks for approval of a new dependency; this is the request. To undo it: move the line back and replace the transport in `app/models/openrouter.py`.
2. **The provider.** Is `coreweave/fp8` on the evidence above acceptable, or should the pin be `fireworks/us` (a stated US-only route, about 3.5 times the input price)? Changing it is two lines in `.env`.
3. **A live smoke run** of `tests/live/` (six calls, a fraction of a cent) to confirm the pin, the routing metadata on a real chat completion and the hand-written fixtures.

## Alternatives considered

- **The `openai` SDK pointed at OpenRouter.** A new dependency that still would not cover the decisions endpoint, and whose typed responses drop the `openrouter_metadata` object the pin check needs.
- **The standard library's `urllib` in threads.** No dependency at all, but blocking calls in a thread pool under an async pipeline, with timeouts and connection reuse written by hand.
- **Trusting the top-level `provider` field.** The first version of this change did. OpenRouter does not document it on chat completions, so the Reviewer asked for the documented routing metadata instead.
- **Accepting the pinned provider when it is merely among the candidates.** Proves nothing about who served the reply; only the `selected` endpoint does.
- **Refusing any other provider among the candidates.** The first fix round did. The Reviewer showed it refuses replies that were routed only to the pinned provider, because an unselected candidate was never called.
- **Trusting the selected endpoint alone after a fallback.** With `attempt` above 1 and no complete history, an earlier attempt could have gone anywhere, so the reply is refused instead.
- **Trusting the request alone for the pin.** Rejected: a routing change or a misread option on OpenRouter's side would then send user data elsewhere without anyone noticing.
- **Deriving the provider's name from its slug.** `google-vertex` answers as "Google" and `mancer` as "Mancer 2", so the name is its own setting.
- **Failing when the default model hears nothing and the fallback fails.** Rejected for music-only reels, see above.
- **Model settings inside `Settings`.** Migrations and the health endpoint would then need an OpenRouter key.
- **A lint rule instead of a test for the import boundary.** The test also checks itself against every import form, and needs no new tool.

## Consequences

- **SAV-14 (pipeline)** takes `ModelClients` from `build_model_clients()`, or the fakes in `app/models/fakes.py` in its tests. It reads `Transcript.is_empty` and `attempts` for speech routing, catches `TranscriptionFailedError` for failures, and owns the "nearly empty" rule and the audio-track check.
- **SAV-17 (response agent)** uses `text_generator` with tools. A reply's `tool_calls` carry parsed arguments; tool results go back as `tool` messages.
- **The Compose `api` service does not receive the model variables yet,** because the API calls no model. The ticket that starts a worker or wires a model into the API adds them to `docker-compose.yml`.
- **Changing the pinned provider** means changing both `TEXT_MODEL_PROVIDER` and `TEXT_MODEL_PROVIDER_NAME`, and recording the new provider's serving-region evidence here. Where a provider serves from can change; re-check it when the pin or the model changes.
- **Response caching** on OpenRouter would make every cached text reply fail, because a replay carries no routing metadata. Leave it off for the text model.
- **`docs/ARCHITECTURE.md` §3.9** says the provider "is verified with the model clients (SAV-10)". This record is that verification so far; the live run is still missing.

## Still open

1. **Nothing here has run against the real API.** The contracts come from OpenRouter's documentation and SAV-7's reports. In particular, the router-metadata guide shows its examples on the Responses API, and gives `provider` values as display names ("OpenAI") without listing CoreWeave's. If a real chat completion places or names things differently, every text call fails with `ProviderPinViolationError` instead of going through unchecked; the live smoke run shows which.
2. **Video input** is sent as a base64 data URL in a `video_url` part. OpenRouter documents that form for the Responses API and says Gemini on Vertex needs base64; SAV-7 sent MP4 files successfully, but its request shape was not kept.
3. **No retry jitter.** Parallel workers that hit a rate limit together wait the same time. Add it if SAV-14's bulk imports show bursts.
4. **An account-wide allowed-provider list** on OpenRouter would be a third guard for the pin. It is an account setting, so it is Muhammed's to set.
