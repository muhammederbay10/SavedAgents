# Savely

Savely indexes posts you save on Instagram (later TikTok, X and Facebook) and lets you search them by chat: places by district, movies by genre or streaming platform, study material by subject, and more.

- Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Rules for contributors and agents: [`AGENTS.md`](AGENTS.md)
- Decision records: [`docs/decisions/`](docs/decisions/)

This repository currently holds the project skeleton (a FastAPI app with a health endpoint, the database schema with its migrations, a local Docker stack and the development tooling), the content-type registry, the Instagram webhook, which stores what users share with the bot and queues the work that follows, and the indexing pipeline, which works out what each saved post is about.

## Requirements

- Python 3.12
- Docker with Compose v2 (`docker compose version`)
- `make`
- ffmpeg, which includes ffprobe, to run the indexing worker on your machine (`brew install ffmpeg` on a Mac). The Docker image installs its own.

## Setup

```bash
git clone https://github.com/muhammederbay10/Savely.git
cd Savely

cp .env.example .env            # local configuration, git-ignored

python3.12 -m venv .venv
source .venv/bin/activate
make install                    # the app plus ruff, mypy and pytest
```

The values in `.env.example` work as they are for local development. To receive real webhooks from Meta, set the two `META_*` values as described under [Instagram webhook](#instagram-webhook).

## Run

```bash
make up
```

This builds and starts four services and waits until all of them are running (and healthy, for the three that have a health check):

| Service | What it is | Address on your machine |
|---|---|---|
| `api` | The FastAPI app | http://localhost:8000 |
| `worker` | The indexing worker (see [Indexing pipeline](#indexing-pipeline)) | none |
| `db` | Postgres 16 with PostGIS and pgvector | `localhost:5432` |
| `redis` | Redis 7 | `localhost:6379` |

Check that the API is up:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Create the database tables (once after the first `make up`, and again after pulling new migrations):

```bash
make migrate
```

Useful commands:

```bash
docker compose logs --follow api   # API logs, one JSON object per line
docker compose logs --follow worker # indexing logs
make down                          # stop everything, keep the database data
docker compose down --volumes      # stop everything and delete the local database
```

If a port is already in use, change `API_PORT`, `POSTGRES_PORT` or `REDIS_PORT` in `.env`.

### Local data from before the rename

The product was called SavedAgent until 18 November 2026. Local data from that time is development data only, and nothing migrates it:

- `.env.example` now names the Postgres user and database `savely`. A `.env` copied earlier still says `savedagent` and keeps working with its existing database; the commands in this README then need its old user. If you switch `.env` to the new values, the existing database volume still has only the old user and database, so Postgres refuses the new ones.
- Ingestion's Redis keys are now named `savely:ingestion:…`. Keys under the old `savedagent:ingestion:…` names are no longer read and expire after `INGESTION_STATE_TTL_SECONDS`.

To start clean, delete both with `docker compose down --volumes` (this deletes all local database and Redis data), then run `make up` and `make migrate`.

### Run the API outside Docker

For auto-reload while you edit code, run only the database and Redis in Docker and the API on your machine:

```bash
docker compose up --detach --wait db redis
make run
```

`make run` reads `.env` and serves on http://localhost:8000.

## Test

```bash
make check
```

`make check` runs the four checks that must pass before any push. You can also run them one at a time:

| Command | Runs |
|---|---|
| `make fmt` | `ruff format .` (rewrites files) |
| `make lint` | `ruff check .` |
| `make typecheck` | `mypy app` |
| `make test` | `pytest` |

Tests never call external services.

Two tests run the real `ffmpeg` and `ffprobe` on a clip they generate. They are **skipped** when the programs are not installed, and pytest lists them with the reason. Every other pipeline test uses a stand-in for them.

### Database tests

The tests for the schema and the migrations need a real Postgres with PostGIS and pgvector. They are **skipped** unless `TEST_DATABASE_ADMIN_URL` is set, and pytest lists every skipped test with the reason. To run them, start the `db` service and point the variable at its `postgres` maintenance database:

```bash
docker compose up --detach --wait db
TEST_DATABASE_ADMIN_URL=postgresql+asyncpg://savely:change-me@localhost:5432/postgres make test
```

Each run creates its own databases named `savely_test_<random>` on that server and drops them at the end. It never reads or changes the `savely` database you develop against. The user in the URL must be allowed to create databases and extensions, which the Compose user is.

### Redis tests

The ingestion tests keep their state in a real Redis, and some check what arq stores there. They are **skipped** unless `TEST_REDIS_URL` is set; most of them also need `TEST_DATABASE_ADMIN_URL`. To run them, start the `redis` service and point the variable at it:

```bash
docker compose up --detach --wait redis
TEST_REDIS_URL=redis://localhost:6379/0 make test
```

The tests use new random ids for every user and job, so they never touch each other's keys, and keys they leave expire. Use a Redis that holds nothing else: one test reads every key to check that "delete my data" erased a user's texts. No worker should be reading that Redis while the tests run. Both variables can be set in the same run.

## Instagram webhook

Users share posts and reels with the bot account by direct message, or save them into a collaborative collection the bot belongs to. Meta sends each message to the API as a `messages` webhook.

| Endpoint | What it does |
|---|---|
| `GET /webhooks/instagram` | Meta's verification handshake. Returns `hub.challenge` when `hub.verify_token` equals `META_WEBHOOK_VERIFY_TOKEN`, and 403 otherwise |
| `POST /webhooks/instagram` | Receives a webhook. Checks `X-Hub-Signature-256` against the raw body with `META_APP_SECRET` (401 if it is missing or wrong), stores the share or routes the text, queues a job and answers 200 |

What happens to a message ([`app/adapters/instagram.py`](app/adapters/instagram.py), decision records 0002, 0003 and 0009):

| Message | Result |
|---|---|
| A reel shared by DM (`ig_reel`) | A saved item with the reel's id, public link and caption |
| A post shared by DM or saved into the collection (`ig_post`, or the older `share`) | A saved item with the post's id (`ig_post` only) and caption. The signed image link is passed to the indexing job and not stored |
| A reel saved into the collection (`unsupported_type`) | A saved item with only its message id. The signed video link is passed to the indexing job and not stored |
| A text sent at most `COLLECTION_HINT_WINDOW_SECONDS` after the user's last share before it (also a repeated share of a saved post, and also when a later share arrived first) | Stored as that share's item's collection, e.g. "For my movies collection", if it has none yet. No chat job |
| Any other text | A chat job for the response agent |
| Anything else (read receipts, reactions, the bot's own messages, other attachments) | Ignored, and logged without its content |

- **First contact:** the first message from an Instagram account creates its `user` and `platform_account` rows.
- **Message id:** every saved item keeps the id of the message it came in with (`source_message_id`), so answers can reply to it (decision record 0004).
- **One item per post and per message:** sharing the same post twice, or a webhook that Meta delivers twice, creates one saved item. Two reels saved into the collection have no post id, so they stay two items (decision record 0007).
- **Jobs:** new items are queued as `index_saved_item` and texts as `answer_chat_message` on Redis, through `arq` ([`app/ingestion/jobs.py`](app/ingestion/jobs.py)). A job holds only ids; the caption, text and media link it works on are in a per-user payload hash ([`app/ingestion/session_state.py`](app/ingestion/session_state.py)). Each job name has its own arq queue (`arq:queue:indexing`, `arq:queue:chat`), so a worker only takes the jobs it can run. The indexing worker runs `index_saved_item` (see [Indexing pipeline](#indexing-pipeline)). The chat worker comes with the response agent (SAV-17); until then chat jobs wait in their queue and expire after a day.
- **Routing is decided once:** each text's route is kept as a receipt by its message id before anything changes, so a repeated delivery is routed the same way, also when a delayed share arrived in between.
- **Ingestion state:** payloads, text receipts and each user's recent shares stay in Redis for `INGESTION_STATE_TTL_SECONDS`, and "delete my data" erases them.
- **Errors:** if Postgres or Redis is down, the endpoint answers 500 and Meta delivers the webhook again. Handling a webhook twice is safe.

### Connect the Meta app

Meta needs a public HTTPS address, so local development uses a tunnel.

1. In `.env`, set `META_APP_SECRET` to the app's secret (Meta app dashboard → **App settings → Basic**) and `META_WEBHOOK_VERIFY_TOKEN` to any long random text.
2. Start the stack and a tunnel:
   ```bash
   make up && make migrate
   ngrok http 8000
   ```
3. In the Meta app go to **Use cases → Customize → API setup with Unified login → Configure webhooks**. Set the callback URL to `https://<your-tunnel>/webhooks/instagram` and the verify token to your `META_WEBHOOK_VERIFY_TOKEN`, then subscribe to `messages`.
4. In **Generate access tokens**, generate a token for the bot account, then switch **Webhook Subscription** off and on again. No direct message arrived before this step (decision record 0002, finding 10).
5. Send the bot a reel from an Instagram Tester account. `docker compose logs --follow api` shows `capture_ingested`.

The dashboard's **Test** button sends a sample in another shape than real messages. It is answered with 200 and ignored.

## Database

The schema is defined twice, and a test keeps the two in step:

- [`app/db/models.py`](app/db/models.py): SQLAlchemy models, used for queries.
- [`app/db/migrations/versions/`](app/db/migrations/versions/): Alembic migrations, the only thing that changes the database.

"Delete my data" is `delete_user_data()` in [`app/db/deletion.py`](app/db/deletion.py). It deletes one user's rows and lowers the item counts of the shared vocabulary. It also takes an eraser for the stores outside Postgres: pass `IngestionDataEraser` from [`app/ingestion/erasure.py`](app/ingestion/erasure.py), which removes the user's queued jobs and ingestion's Redis state. Ingestion and deletion lock the same user row, so they never overlap. The shared tables (entities, streaming availability, vocabulary and aliases) keep their rows.

| Command | What it does |
|---|---|
| `make migrate` | Apply every migration (`alembic upgrade head`) |
| `alembic downgrade base` | Drop every table the migrations created. **This deletes the data in them** |
| `alembic revision -m "add x"` | Start a new, empty migration to fill in by hand |
| `alembic check` | Fail if the models and the database differ |

Alembic reads `DATABASE_URL` and `EMBEDDING_DIMENSION` through `app/config.py`, from `.env` or the environment. Run it from the repository root.

The first migration creates the two embedding columns (`saved_item.embedding` and `vocabulary.embedding`) with `EMBEDDING_DIMENSION` components. The embedding model is `openai/text-embedding-3-large` at **1024 dimensions** (Muhammed's choice in SAV-7), so set `EMBEDDING_DIMENSION=1024`, as `.env.example` does. Both columns are then `vector(1024)`, and the database rejects a vector of any other length.

The dimension stays a setting, so another model can be tried on a fresh database. To change it on a database that is already migrated, write a new migration that resizes both columns, and embed every saved item and vocabulary value again.

## Spikes

[`scripts/spikes/`](scripts/spikes/README.md) holds small experiments that back a decision record. `model_choices` compares speech-to-text and embedding models through OpenRouter (SAV-7, [record 0008](docs/decisions/0008-model-choices.md)). By default it sends nothing; a live run costs money, reads `OPENROUTER_API_KEY` from the environment and needs the owner's approval first.

## Configuration

All configuration comes from environment variables. Locally they live in `.env`, and `.env.example` lists every variable. The variables of the model clients have their own table under [Model settings](#model-settings).

| Variable | Used by | Meaning |
|---|---|---|
| `LOG_LEVEL` | API | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `DATABASE_URL` | API | Postgres URL, e.g. `postgresql+asyncpg://user:password@host:5432/db` |
| `REDIS_URL` | API | Redis URL, e.g. `redis://host:6379/0` |
| `EMBEDDING_DIMENSION` | API, migrations | Length of the embedding vectors, 1 to 2000. Must match the embedding model: `1024` for `openai/text-embedding-3-large` (see [Database](#database)) |
| `META_APP_SECRET` | API | The Meta app's secret. Every webhook is signed with it. **Secret** |
| `META_WEBHOOK_VERIFY_TOKEN` | API | The text Meta repeats in the verification handshake. You choose it and enter it in the Meta app. **Secret** |
| `COLLECTION_HINT_WINDOW_SECONDS` | API | A text sent this many seconds or less after a share becomes that item's collection hint. A whole number above 0 |
| `INGESTION_STATE_TTL_SECONDS` | API | How long ingestion keeps a user's job payloads, text receipts and recent shares in Redis. At least the hint window; 129600 (36 hours, Meta's documented retry period) in `.env.example` |
| `INSTAGRAM_GRAPH_API_URL` | DM replies | Graph API base URL with its version, e.g. `https://graph.facebook.com/v27.0` (see [DM replies](#dm-replies)) |
| `INSTAGRAM_ACCOUNT_ID` | DM replies | The bot's Instagram professional account id |
| `INSTAGRAM_ACCESS_TOKEN` | DM replies | The bot's access token for the Send API. A secret |
| `INSTAGRAM_REPLY_WINDOW_HOURS` | DM replies | How long after the user's last message the bot may reply: more than 0 and at most `24` (Instagram's limit) |
| `SEND_API_TIMEOUT_SECONDS` | DM replies | Time limit of one Send API request |
| `SEND_API_MAX_ATTEMPTS`, `SEND_API_RETRY_BASE_DELAY_SECONDS`, `SEND_API_RETRY_MAX_DELAY_SECONDS` | DM replies | Retries when Meta throttles a message or the connection never opens: total attempts, first wait, longest wait |
| `TEST_DATABASE_ADMIN_URL` | Tests only | Postgres server for the database tests (see [Database tests](#database-tests)). Not read by the API |
| `TEST_REDIS_URL` | Tests only | Redis server for the Redis tests (see [Redis tests](#redis-tests)). Not read by the API |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Docker Compose | Credentials and database name for the `db` container |
| `API_PORT`, `POSTGRES_PORT`, `REDIS_PORT` | Docker Compose | Ports published on `127.0.0.1` |

The API settings are defined in [`app/config.py`](app/config.py): `Settings` for the API and `MessengerSettings` for DM replies, which is only read when the bot sends a DM, so `/health` and the migrations don't need the token. None of them has a default, so the API refuses to start when one is missing or invalid. The error names the variable but never prints its value, because a URL can contain a password. The two secrets are also hidden when the settings are printed. Under Docker Compose, `DATABASE_URL` and `REDIS_URL` are built in `docker-compose.yml` from the `POSTGRES_*` values, so the two in `.env` are only used by `make run`.

## DM replies

[`app/agent/messenger.py`](app/agent/messenger.py) sends the bot's DMs through the Instagram Send API, and [`app/agent/reply_templates.py`](app/agent/reply_templates.py) holds the fixed wording (decision records [0004](docs/decisions/0004-reply-to-saved-reels.md) and [0011](docs/decisions/0011-dm-replies.md)). The indexing pipeline sends the save confirmation with them; the response agent (SAV-17) will send the rest.

```python
import httpx

from app.agent.messenger import InstagramMessenger
from app.agent.reply_templates import ResultLine, reply_wording, result_reply
from app.config import get_messenger_settings

wording = reply_wording(user.language, localized=agent_wording)  # see "Languages" below
async with httpx.AsyncClient() as http_client:
    messenger = InstagramMessenger(http_client, get_messenger_settings())
    line = ResultLine(position=1, total=3, name="Kronotrop", area="Beşiktaş", open_now=True)
    result = await messenger.send(
        recipient_id=account.platform_user_id,          # the user's IGSID
        message=result_reply(line, item.source_message_id, wording),
        last_user_message_at=chat_message.received_at,  # Meta's time of their last message
    )
```

- **Replies to saved reels.** A message with `reply_to_message_id` is sent with `reply_to.mid`, so Instagram shows the saved reel above the line.
- **24-hour window.** Before every request, retries included, the client checks the time since the user's last message. Past `INSTAGRAM_REPLY_WINDOW_HOURS` (at most 24; larger values are refused) it sends nothing more: `send` returns `OUTSIDE_REPLY_WINDOW` with the number of requests already made, and logs `dm_skipped_outside_reply_window`. Meta's own "outside the window" answer is reported the same way.
- **Retries.** Only failures that show the message was not taken are retried, with exponential backoff up to `SEND_API_MAX_ATTEMPTS`: Meta's throttling errors and connections that never opened. A timeout after sending or any 5xx, 503 included, may have delivered the message, so it raises `MessengerDeliveryUnknownError` and is not repeated. This avoids repeating a message that may already have been delivered. Meta documents no guarantee about it, so a duplicate is still possible if Meta reports throttling for a message it did take. Every other failure raises a `MessengerError` subclass.
- **Languages.** Every template takes a `ReplyWording`, the bot's own words in the user's language, from `reply_wording(language, localized=None)`:
  - wording passed as `localized` is used as it is, for any language;
  - otherwise the built-in English or Turkish wording, chosen by the primary subtag (`tr-TR` → Turkish);
  - otherwise `ReplyWordingUnavailableError`. Nothing falls back to English silently.

  For other languages the caller supplies the wording. SAV-17's response agent, which already writes replies in the user's language, owns producing it; `has_built_in_wording(language)` tells it when it must. Supplied wording is validated when it is built: every field non-empty, and exactly the placeholders listed on `ReplyWording`, as bare names such as `{item}`. A format spec (`{item:.0}`), a conversion (`{item!r}`), an index or an attribute is refused, so a caller's value is always shown whole. Names, open categories, option labels, count labels and other values are shown exactly as passed in.
- **Logs** hold the kind of message, the attempt, the HTTP status and Meta's error code, never the recipient, a message id, the text or the token.

## Logging

Logs are structured: one JSON object per line, written to stdout. Put data in `extra`, not in the message:

```python
import logging

logger = logging.getLogger(__name__)
logger.info("item_indexed", extra={"item_id": str(item.id), "step": "extract"})
```

Never log secrets, tokens, raw webhook bodies or personal data.

What the logging setup does for you:

- `LOG_LEVEL` applies to every logger, including uvicorn's and other libraries'.
- There is no access log. Uvicorn's access records hold the client's address and the full query string, so they are switched off. To see a request in the logs, log it from the application code, without personal data.
- The API is started with `--ws none`. It has no WebSocket endpoints, and uvicorn would otherwise log the address and query string of every WebSocket handshake.

## Indexing pipeline

Every saved item is understood once, when it is saved (`docs/ARCHITECTURE.md` §3.3, decision record [0012](docs/decisions/0012-indexing-pipeline.md)). The webhook queues one `index_saved_item` job per item, and the worker runs these steps:

1. **Fetch:** download the media from the link the platform sent and keep a 360p copy, a still picture, or a smaller image. A reel shared by DM comes without a file, so it is indexed from its caption only. Nothing is ever read from a post's page.
2. **Classify:** one decision-model call on the media, the caption and the collection hint: the built-in type (or none of them), and whether the post lists several things.
3. **Speech-to-text:** ffprobe decides. A file without an audio track has no speech. A file with one is always transcribed, by the default model and then the fallback. A final transcript with fewer than `TRANSCRIPT_MIN_SPEECH_CHARACTERS` letters and digits counts as no speech; letters of every script count, and so do digits and the marks that combine with letters, while spaces, punctuation and symbols such as ♪ don't. A model that fails is never taken as silence: the job runs again.
4. **Extract:** the text model fills in the type's schema. When a type needs an entity and none was named, the escalation model watches the media.
5. **Normalize:** open values (category, tags, cuisine, subject…) go through the shared vocabulary: exact hit, privacy gate, then match-or-new (§3.4).
6. **Embed** the summary and transcript, mark the item `done`, and **confirm** by DM how it was saved, in the user's language, or in English while it isn't known.

Each step's result is kept in `enrichment_run`, so a job that runs again continues where it stopped. A run that can't read its payload or item, or that passes `INDEXING_RUN_DEADLINE_SECONDS`, runs again too, and an ffmpeg or ffprobe run it was waiting for is stopped and reaped first. That deadline starts before the payload is read, so a slow start (for example, the user's row locked by "delete my data") uses the same budget as the steps. An item gets `INDEXING_MAX_TRIES` runs in all, counted on the item across requeues; the last marks it `failed` as well as it can. Whatever a job couldn't write, the **stuck-item sweeper** repairs: an arq cron in the worker that, every `INDEXING_SWEEP_INTERVAL_SECONDS`, finds items still `pending` or `enriching` that no job has worked on for `INDEXING_STALE_AFTER_SECONDS`, and queues each again (tries left, payload still there) or marks it `failed`. It reads Postgres only, takes each owner's row lock with `SKIP LOCKED`, and is safe with several workers. The worker reads only the indexing queue; chat jobs wait in theirs. Logs hold, for each item, its type, whether it was escalated, and the cost of its model calls.

Run the worker on your machine (it reads `.env`, and needs ffmpeg, Postgres and Redis):

```bash
docker compose up --detach --wait db redis
python -m app.pipeline.worker
```

Under Compose, the `worker` service runs the same command from the image, which includes ffmpeg. It reads `.env` whole, because it needs nearly every setting, and overrides the database and Redis URLs and the media folder for the Compose network. With the dummy `OPENROUTER_API_KEY` from `.env.example`, every model call is refused, and the items it picks up end as `failed`.

**Media** is kept in `MEDIA_STORAGE_DIR` (`./media` on your machine, git-ignored; the `media_data` volume under Compose), in a folder per user and item. "Delete my data" removes the user's folder. A hosted object store comes with deployment.

**Prompts** are in [`app/pipeline/prompts.py`](app/pipeline/prompts.py), in one `PipelinePrompts` object with a version. The version is stored with each step's result. Change the version with any change to the wording, and score the change on the eval set (AGENTS.md §4).

The worker reads `Settings`, `ModelSettings`, `MessengerSettings` and `PipelineSettings` (all in [`app/config.py`](app/config.py)). The pipeline's own variables:

| Variable | Meaning |
|---|---|
| `MEDIA_STORAGE_DIR` | The folder that stands in for object storage |
| `MEDIA_DOWNLOAD_TIMEOUT_SECONDS`, `MEDIA_MAX_DOWNLOAD_BYTES` | Time limit and largest size of one download. Larger media is skipped and the item indexed from its text |
| `MEDIA_STORED_HEIGHT_PIXELS` | Height of the stored copies (360). Smaller media keeps its size |
| `FFMPEG_PATH`, `FFPROBE_PATH`, `MEDIA_TOOL_TIMEOUT_SECONDS` | The two programs, and the time limit of one run |
| `MEDIA_TOOL_STOP_TIMEOUT_SECONDS` | A run that times out, or is cancelled by the run's deadline, stops its own child: SIGTERM, then SIGKILL if it hasn't exited within this time, then waits up to this time again for it to be reaped. A cancellation during that clean-up doesn't cut it short |
| `TRANSCRIPT_MIN_SPEECH_CHARACTERS` | The near-empty rule above |
| `CLASSIFY_IS_LIST_MIN_PROBABILITY` | A post counts as a list from this probability of yes |
| `EXTRACTION_TEMPERATURE`, `EXTRACTION_MAX_OUTPUT_TOKENS` | Sampling and reply length of extraction and the escalation |
| `VOCAB_PRIVACY_MIN_PROBABILITY` | Wording enters the shared vocabulary only with at least this probability of being generic |
| `VOCAB_ALIAS_MIN_PROBABILITY`, `VOCAB_ALIAS_MIN_USERS` | A candidate alias is verified after matches this confident from this many different users |
| `VOCAB_MAX_VALUE_LENGTH`, `VOCAB_PHONE_MIN_DIGITS` | Longer wording, or wording with a phone-like run of this many digits, never enters the vocabulary |
| `VOCAB_MATCH_CANDIDATES` | How many nearby values the decision model chooses from |
| `INDEXING_CONCURRENCY` | Jobs one worker runs at once; a bulk import fans out over every slot of every worker |
| `INDEXING_MAX_TRIES`, `INDEXING_RETRY_DELAY_SECONDS` | Runs of one job, and the wait before the next run, multiplied by the run's number |
| `INDEXING_RUN_DEADLINE_SECONDS` | How long one run may take, from before it reads the payload to the end of its last step. A run that passes it stops itself, records where it was, and runs again; the last try marks the item `failed` |
| `INDEXING_FAILURE_RECORDING_SECONDS` | How long recording a failure may take after that, from one deadline. If the database doesn't answer in time, the failure is only logged, the job still ends itself, and the sweeper repairs the item later |
| `INDEXING_CLEANUP_RESERVE_SECONDS` | Time kept free for clean-up after a stopped run, at least twice `MEDIA_TOOL_STOP_TIMEOUT_SECONDS` |
| `INDEXING_JOB_TIMEOUT_SECONDS` | arq's own limit on one run, a backstop that cancels without a retry. Must be longer than the run deadline, the recording time and the clean-up reserve together |
| `INDEXING_SWEEP_INTERVAL_SECONDS` | How often the stuck-item sweeper runs. It must divide a minute, or be whole minutes that divide an hour |
| `INDEXING_STALE_AFTER_SECONDS` | How long an unfinished item goes without any job working on it before the sweeper takes it. Longer than a whole run plus every retry wait |
| `INDEXING_SWEEP_BATCH_SIZE` | The most stuck items one sweep handles |

## Models

Every model call goes through [OpenRouter](https://openrouter.ai) with one key, and every model sits behind a small interface in [`app/models/base.py`](app/models/base.py). Which model serves a role is a setting, so a model is swapped in `.env` without a code change. The choices and their measurements are in decision record 0008 (SAV-7); how the clients work is in [0010](docs/decisions/0010-model-clients.md).

| Interface | Role | Model (setting) | Endpoint |
|---|---|---|---|
| `Classifier.decide` | Fixed decisions: typed questions answered with probabilities | `google/gemini-decision-1` (`DECISION_MODEL`) | `POST /api/alpha/decisions`, its own contract, not chat completions |
| `Transcriber.transcribe` | Speech-to-text | `microsoft/mai-transcribe-2` (`TRANSCRIPTION_MODEL`), then `openai/gpt-transcribe` (`TRANSCRIPTION_FALLBACK_MODEL`) | `POST /api/v1/audio/transcriptions` |
| `Generator.generate` | Extraction and the response agent: text, JSON that follows a schema, tool calls | `deepseek/deepseek-v4.1-flash` (`TEXT_MODEL`), pinned to one provider | `POST /api/v1/chat/completions` |
| `Generator.generate` | Escalation: watch the video when the text gave no entity | `google/gemini-2.5-flash` (`ESCALATION_MODEL`) | `POST /api/v1/chat/completions`, with the video inline |
| `Embedder.embed` | Embeddings | `openai/text-embedding-3-large` (`EMBEDDING_MODEL`) at `EMBEDDING_DIMENSION` | `POST /api/v1/embeddings` |

```python
from app.config import get_model_settings
from app.models.base import GenerationRequest, Message, YesNoQuestion
from app.models.factory import build_model_clients, open_http_client

settings = get_model_settings()
async with open_http_client(settings) as http_client:
    models = build_model_clients(settings, http_client)

    decision = await models.classifier.decide(
        ["the caption", video], [YesNoQuestion("is_list", "Is this a list of several things?")]
    )
    transcript = await models.transcriber.transcribe(audio)
    reply = await models.text_generator.generate(
        GenerationRequest(messages=[Message(role="user", content="…")])
    )
    vectors = await models.embedder.embed(["a cosy café"])
```

**The boundary.** Code outside `app/models/` imports only `app.models.base` (the interfaces), `app.models.errors` and `app.models.factory`. It never imports a model SDK, a client class or the HTTP transport. `tests/test_model_boundary.py` fails if it does.

**In tests, use the fakes** in [`app/models/fakes.py`](app/models/fakes.py): `FakeClassifier`, `FakeTranscriber`, `FakeGenerator` and `FakeEmbedder`. They make no network call, return what the test scripted (or raise a scripted error), and record the calls they received.

**What the results tell you**

- **Decisions:** one answer per question, by key. A question the model refused, left out or answered unusably comes back as `Unanswered`; the others stay valid.
- **Transcripts:** `model` is the model whose answer it is, and `attempts` lists every model that was asked. `language`, `segments` and `words` are `None` when the model did not return them, which is always the case for the fallback model. **An empty transcript is a result, not an error:** it means no model heard words. A **failure** is raised as `TranscriptionFailedError`, and only when both models failed. The full table is in [record 0010](docs/decisions/0010-model-clients.md#speech-to-text-an-empty-transcript-is-a-result-a-failure-is-an-error).
- **Generation:** `structured` holds the parsed JSON when a schema was requested. `tool_calls` holds the calls with parsed arguments.
- **Embeddings:** every vector has exactly `EMBEDDING_DIMENSION` components, or the call raises `EmbeddingDimensionError`.

**Errors** are all subclasses of `ModelError` ([`app/models/errors.py`](app/models/errors.py)). A call that times out, is rate-limited or hits a server error is retried with a doubling wait (`OPENROUTER_MAX_ATTEMPTS`, `OPENROUTER_RETRY_*`); anything else fails at once.

| Error | Meaning |
|---|---|
| `ModelRequestError` | The request was rejected. Subclasses: `ModelAuthenticationError` (key refused), `ModelCreditsError` (out of credits) |
| `ModelRateLimitError`, `ModelUnavailableError` | Still rate-limited, or timeouts and server errors, after the last attempt |
| `ProviderRouteUnavailableError` | The pinned provider cannot serve the call, and no other provider is allowed |
| `ProviderPinViolationError` | The reply does not prove that the pinned provider, and only it, served it. The reply is discarded |
| `ModelResponseError` | The answer was unusable. Subclasses: `SchemaOutputError`, `EmbeddingDimensionError` |
| `TranscriptionFailedError` | Both speech-to-text models failed |

**Nothing private is logged.** Logs and error messages hold the operation, the model, the HTTP status and the attempt number. They never hold the key, a caption, a transcript, a message, media, or a provider's own error text.

### The DeepSeek provider pin

DeepSeek's own API is hosted in China, and OpenRouter by default sends a model to whichever provider it likes. So the text model is **pinned to one provider endpoint hosted outside China**, and the pin is a setting:

```bash
TEXT_MODEL_PROVIDER=coreweave/fp8     # the endpoint's OpenRouter slug
TEXT_MODEL_PROVIDER_NAME=CoreWeave    # the name OpenRouter reports for it in routing metadata
```

- **Every request allows only that endpoint:** `provider: {"only": [slug], "order": [slug], "allow_fallbacks": false}`.
- **Every reply must prove where it was served.** Each request sends `X-OpenRouter-Metadata: enabled`, and OpenRouter then adds `openrouter_metadata` to the reply. The reply is used only if exactly one endpoint is `selected`, it is the pinned provider with the configured model, and every attempt the router made went to that provider. After more than one attempt the history must be complete (one entry per attempt, the last one successful), or the earlier attempts are unidentified. Anything else (no metadata, malformed or ambiguous metadata, a missing or invalid attempt number, missing or incomplete history, another provider, a top-level `provider` field that disagrees) discards the reply with `ProviderPinViolationError`. Another provider listed only as an unselected candidate is fine: it was considered, not called.
- **Cache replays are refused.** OpenRouter strips the metadata from them, so they can't be proven. Keep response caching off for the text model.
- **No route means an error, never another host.** If the endpoint cannot serve a call (OpenRouter answers 404 or 503; a 503 is retried first), `generate` raises `ProviderRouteUnavailableError`.

**Why CoreWeave** (evidence read on 17 Nov 2026; the full comparison is in [record 0010](docs/decisions/0010-model-clients.md#the-provider-that-serves-deepseek)):

- OpenRouter's provider list gives CoreWeave `datacenters: ["US"]`.
- CoreWeave's own list of all its availability zones has 60 zones in the United States, Canada and Europe, and none in China or elsewhere in Asia.
- CoreWeave lists DeepSeek V4.1 Flash among its hosted models, and OpenRouter's endpoint supports JSON-schema output and tool calls.

No page says which zone serves the endpoint, and **no live call has confirmed the route yet**. The choice is waiting for the owner's confirmation. To change the provider, change both variables and add the new provider's serving-region evidence to a decision record. A company's home country alone is not evidence.

### Model settings

The model clients read these variables through `ModelSettings` in [`app/config.py`](app/config.py), with `get_model_settings()`. It is a separate class from the API's `Settings`: the API and the migrations call no model, so they start without these. The same rules apply: no variable has a default, and an error names the variable without printing its value. The values in `.env.example` are the approved models; only `OPENROUTER_API_KEY` needs a real value, and only for real model calls.

| Variable | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | The one key for every model call. Never commit a real one |
| `OPENROUTER_BASE_URL` | Base URL for chat completions, transcriptions and embeddings: `https://openrouter.ai/api/v1`. Must be `https` |
| `OPENROUTER_DECISIONS_URL` | Full URL of the decisions endpoint, which is not chat completions: `https://openrouter.ai/api/alpha/decisions`. Must be `https` |
| `OPENROUTER_MAX_ATTEMPTS` | How often one call is tried in total when it times out, is rate-limited or hits a server error. At least 1 |
| `OPENROUTER_RETRY_BASE_DELAY_SECONDS`, `OPENROUTER_RETRY_MAX_DELAY_SECONDS` | The wait before the second try, which doubles each time, and the longest wait |
| `DECISION_MODEL`, `DECISION_TIMEOUT_SECONDS` | Decision model id (`google/gemini-decision-1`) and the time limit of one request |
| `TEXT_MODEL`, `TEXT_TIMEOUT_SECONDS` | Text model id (`deepseek/deepseek-v4.1-flash`) and the time limit of one request |
| `TEXT_MODEL_PROVIDER` | The one provider endpoint that may serve the text model, as an OpenRouter slug (`coreweave/fp8`). Must be hosted outside China. See [The DeepSeek provider pin](#the-deepseek-provider-pin) |
| `TEXT_MODEL_PROVIDER_NAME` | The name OpenRouter reports for that provider in a reply's routing metadata (`CoreWeave`). A reply not proven to come from it is discarded |
| `ESCALATION_MODEL`, `ESCALATION_TIMEOUT_SECONDS` | Video model id (`google/gemini-2.5-flash`) and the time limit of one request |
| `TRANSCRIPTION_MODEL`, `TRANSCRIPTION_RESPONSE_FORMAT` | Default speech-to-text model (`microsoft/mai-transcribe-2`) and its response format: `verbose_json` (with language and timestamps) or `json` |
| `TRANSCRIPTION_FALLBACK_MODEL`, `TRANSCRIPTION_FALLBACK_RESPONSE_FORMAT` | The model asked when the default fails or returns no words (`openai/gpt-transcribe`, which only accepts `json`) |
| `TRANSCRIPTION_TIMEOUT_SECONDS` | Time limit of one transcription request, for either model |
| `EMBEDDING_MODEL`, `EMBEDDING_TIMEOUT_SECONDS` | Embedding model id (`openai/text-embedding-3-large`) and the time limit of one request. The vector size is `EMBEDDING_DIMENSION` |
| `EMBEDDING_DIMENSION` | Shared with the database (see [Configuration](#configuration)): the vector size the embedding model is asked for, and the size every returned vector must have |

The Compose `api` service does not receive these variables, because the API calls no model. The `worker` service reads them from `.env` (see [Indexing pipeline](#indexing-pipeline)).

### Live smoke tests

`tests/live/` holds one real call per model client. They are **skipped** unless both `LIVE_MODEL_TESTS=1` and `OPENROUTER_API_KEY` are set, so `make check` never spends money. They send only invented text and one second of generated silence, and cost a fraction of a cent together. **A live run needs the owner's approval first.**

Tests do not read `.env`, so load it into your shell first:

```bash
set -a; source .env; set +a
LIVE_MODEL_TESTS=1 pytest -m live
```

The text-model test also checks, from the reply's routing metadata, that the pinned provider served it. Sending a video to the escalation model is not covered: the test sends text only.

## Content types

Every saved post gets an open **category** and **tags**, written by the text model in plain words and in any language ("restaurant", "car review", "ギターレッスン"). Nothing is picked from a list in code, and nothing is tied to a city, a country or a language (decision record 0005, SAV-26).

A **built-in type** exists only where a kind of post gets extra behaviour: more fields, a resolver or more search filters. Each one is a module in `app/types/` that declares a `ContentType`, and [`app/types/registry.py`](app/types/registry.py) lists them. The rest of the code asks the registry and reads the declaration. It never checks for a type by name, and a test (`tests/test_content_type_registry.py`) fails if it does.

| Built-in type | Fields added to the schema | Resolver | Extra search filters |
|---|---|---|---|
| `place` | per venue: `name`, `area_hint`, `cuisine` | `google_places` | `area`, `near`, `open_now`, `cuisine`, `price` |
| `movie` (also series) | per title: `title`, `year_hint` | `tmdb` | `genre`, `year`, `streaming`, `watched` |
| `study` | `subject`, `topic`, `format`, `key_points`, `resources`, `exam` | none | `subject`, `topic`, `format`, `exam` |
| *(no built-in type)* | none: `summary`, `category` and `tags` only | none | none |

A post that fits no built-in type is not an error and is not pushed into the nearest one. The decision model answers "none of these", the post has no type, and it keeps its summary, category and tags:

```python
from app.types.registry import CONTENT_TYPES

place = CONTENT_TYPES.get("place")
place.extraction_schema.model_validate(text_model_output)       # raises on malformed output
[search_filter.name for search_filter in place.search_filters]

CONTENT_TYPES.extraction_schema("place")   # PlaceExtraction
CONTENT_TYPES.extraction_schema(None)      # Extraction: summary, category, tags
```

A declaration has these parts (all defined in [`app/types/base.py`](app/types/base.py)):

| Part | What it is |
|---|---|
| `name`, `description` | The name stored on a saved item, and the text the decision model sees when it picks a post's type |
| `extraction_schema` | The Pydantic model the text model fills in. It extends `Extraction`, so it always has `summary`, `category` and `tags`. Unknown keys, missing keys, blank text and wrong shapes are rejected. The values themselves are open |
| `resolver` | The name of the resolver in `app/resolvers/` that looks up the type's entities, or `None` |
| `search_filters` | The filters the type adds to search. `category` and `tags` work for every item and are not declared per type |

**Open values and the vocabulary.** A field such as `cuisine` or `subject` is open text. To keep "korean", "Korean food" and "Kore mutfağı" from becoming three values, the field is marked with `Vocabulary("cuisine")`. The normalization step (a later ticket) reads the marks with `vocabulary_fields(...)` and matches each new value against the shared vocabulary. A filter of kind `value` names the same vocabulary field, so a question in one language finds a value saved in another.

| Filter kind | Takes |
|---|---|
| `value` | An open value, normalized to one that exists in the user's own saves |
| `area` | A place name at any level, anywhere: country, region, city, district or neighbourhood |
| `location` | A point and a distance around it |
| `boolean`, `integer` | True or false, or a whole number |
| `text` | Free text that is matched as it is |

### How to add a new type

Add a type only when it needs something that the open category can't give: its own fields, a resolver or its own filters. Example: a `recipe` type.

1. **Create `app/types/recipe.py`.** Copy `study.py` for a type without a resolver, or `movie.py` for one with a resolver.
2. **Write the extraction schema.** Extend `Extraction` (it already has `summary`, `category` and `tags`) and give every field a `description`, because the text model reads it. If a post points at real-world things that a resolver looks up, extend `EntityExtraction` instead and put them in `entities`, with a mention class that extends `EntityMention`.
3. **Keep values open.** Never add an enum or a list of real-world values (diets, cuisines, cities…). Use `NonEmptyText`, and mark a field whose values should be kept consistent: `diet: Annotated[NonEmptyText | None, Vocabulary("diet")]`. Write examples in the description, not rules, and don't assume a language or a country.
4. **Declare the `ContentType`:** the name, a one-line description, the resolver name (or `None`) and the search filters. Give a `value` filter the same vocabulary field as the schema field it searches.
5. **Register it:** add it to `CONTENT_TYPES` in `app/types/registry.py`. This is the only existing file you change.
6. **Test it:** in `tests/test_content_type_schemas.py`, add a good example, values nobody listed in several languages, and malformed output. In `tests/test_content_type_registry.py`, add the expected resolver, filters and vocabulary fields.
7. **Update the docs:** the table above and `docs/ARCHITECTURE.md` §3.4.

A declaration that can't work fails when the module is imported, with a `ContentTypeDefinitionError`: a repeated filter or type name, a vocabulary field on a filter that is not a `value`, or a resolver on a type whose schema has no `entities` (or the reverse).

## Project layout

```
app/
  main.py             # FastAPI app factory and GET /health
  config.py           # settings from environment variables
  logging_config.py   # JSON logging setup
  adapters/
    base.py           # CaptureEvent and ChatMessage: what every adapter outputs
    instagram.py      # signature check, webhook parsing → CaptureEvent / ChatMessage
  api/
    webhooks.py       # GET and POST /webhooks/instagram
  ingestion/
    service.py        # stores captures, routes texts, queues jobs
    repository.py     # users on first contact, saved items, collection hints
    jobs.py           # job names, ids and queues (one arq queue per job name)
    session_state.py  # per-user state in Redis: payloads, text receipts, recent shares
    erasure.py        # erases a user's Redis state and jobs for "delete my data"
  pipeline/           # indexing: steps, prompts, vocabulary, the arq worker (see "Indexing pipeline")
  types/              # content-type registry: one module per built-in type
  models/             # model interfaces, OpenRouter clients, fakes (see "Models")
  agent/
    messenger.py      # Instagram Send API client: DMs, quick replies, replies to saved reels
    reply_templates.py # fixed DM wording in the user's language
  db/
    models.py         # SQLAlchemy models
    session.py        # the API's engine and session factory
    types.py          # column types for pgvector and PostGIS
    deletion.py       # "delete my data" for one user
    migrations/       # Alembic environment and revisions
alembic.ini           # Alembic configuration
scripts/spikes/       # reproducible experiments behind decision records (see its README)
tests/                # pytest suite
docker/postgres/      # Postgres 16 image with PostGIS and pgvector
docker-compose.yml    # local stack: db, redis, api, worker
Dockerfile            # the image of the API and the worker
Makefile              # install, fmt, lint, typecheck, test, check, run, migrate, up, down
docs/
  ARCHITECTURE.md     # the design (source of truth)
  decisions/          # decision records
```

The full target layout is in [`docs/ARCHITECTURE.md` §6](docs/ARCHITECTURE.md#6-repository-layout). Folders are added when a ticket needs them.
