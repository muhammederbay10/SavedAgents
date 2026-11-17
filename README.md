# SavedAgent

SavedAgent indexes posts you save on Instagram (later TikTok, X and Facebook) and lets you search them by chat: places by district, movies by genre or streaming platform, study material by subject, and more.

- Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Rules for contributors and agents: [`AGENTS.md`](AGENTS.md)
- Decision records: [`docs/decisions/`](docs/decisions/)

This repository currently holds the project skeleton (a FastAPI app with a health endpoint, the database schema with its migrations, a local Docker stack and the development tooling), the content-type registry and the Instagram webhook, which stores what users share with the bot and queues the work that follows.

## Requirements

- Python 3.12
- Docker with Compose v2 (`docker compose version`)
- `make`

## Setup

```bash
git clone https://github.com/muhammederbay10/SavedAgents.git
cd SavedAgents

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

This builds and starts three services and waits until all of them are healthy:

| Service | What it is | Address on your machine |
|---|---|---|
| `api` | The FastAPI app | http://localhost:8000 |
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
make down                          # stop everything, keep the database data
docker compose down --volumes      # stop everything and delete the local database
```

If a port is already in use, change `API_PORT`, `POSTGRES_PORT` or `REDIS_PORT` in `.env`.

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

### Database tests

The tests for the schema and the migrations need a real Postgres with PostGIS and pgvector. They are **skipped** unless `TEST_DATABASE_ADMIN_URL` is set, and pytest lists every skipped test with the reason. To run them, start the `db` service and point the variable at its `postgres` maintenance database:

```bash
docker compose up --detach --wait db
TEST_DATABASE_ADMIN_URL=postgresql+asyncpg://savedagent:change-me@localhost:5432/postgres make test
```

Each run creates its own databases named `savedagent_test_<random>` on that server and drops them at the end. It never reads or changes the `savedagent` database you develop against. The user in the URL must be allowed to create databases and extensions, which the Compose user is.

### Redis tests

Two tests check that a job really lands in Redis and that arq refuses a second job with the same id. They are **skipped** unless `TEST_REDIS_URL` is set. To run them, start the `redis` service and point the variable at it:

```bash
docker compose up --detach --wait redis
TEST_REDIS_URL=redis://localhost:6379/0 make test
```

Each test queues one job under a random id and removes it again. No worker should be reading that Redis while the tests run. Both variables can be set in the same run.

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
| A text sent at most `COLLECTION_HINT_WINDOW_SECONDS` after a share | Stored as that item's collection, e.g. "For my movies collection". No chat job |
| Any other text | A chat job for the response agent |
| Anything else (read receipts, reactions, the bot's own messages, other attachments) | Ignored, and logged without its content |

- **First contact:** the first message from an Instagram account creates its `user` and `platform_account` rows.
- **Message id:** every saved item keeps the id of the message it came in with (`source_message_id`), so answers can reply to it (decision record 0004).
- **One item per post and per message:** sharing the same post twice, or a webhook that Meta delivers twice, creates one saved item. Two reels saved into the collection have no post id, so they stay two items (decision record 0007).
- **Jobs:** new items are queued as `index_saved_item` and texts as `answer_chat_message` on Redis, through `arq` ([`app/ingestion/jobs.py`](app/ingestion/jobs.py)). The workers that run them come with the indexing pipeline and the response agent. Until then the jobs wait in Redis and expire after a day, and the items stay `pending`.
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

"Delete my data" is `delete_user_data()` in [`app/db/deletion.py`](app/db/deletion.py). It deletes one user's rows and lowers the item counts of the shared vocabulary. The shared tables (entities, streaming availability, vocabulary and aliases) keep their rows.

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

All configuration comes from environment variables. Locally they live in `.env`, and `.env.example` lists every variable.

| Variable | Used by | Meaning |
|---|---|---|
| `LOG_LEVEL` | API | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `DATABASE_URL` | API | Postgres URL, e.g. `postgresql+asyncpg://user:password@host:5432/db` |
| `REDIS_URL` | API | Redis URL, e.g. `redis://host:6379/0` |
| `EMBEDDING_DIMENSION` | API, migrations | Length of the embedding vectors, 1 to 2000. Must match the embedding model: `1024` for `openai/text-embedding-3-large` (see [Database](#database)) |
| `META_APP_SECRET` | API | The Meta app's secret. Every webhook is signed with it. **Secret** |
| `META_WEBHOOK_VERIFY_TOKEN` | API | The text Meta repeats in the verification handshake. You choose it and enter it in the Meta app. **Secret** |
| `COLLECTION_HINT_WINDOW_SECONDS` | API | A text sent this many seconds or less after a share becomes that item's collection hint. A whole number above 0 |
| `TEST_DATABASE_ADMIN_URL` | Tests only | Postgres server for the database tests (see [Database tests](#database-tests)). Not read by the API |
| `TEST_REDIS_URL` | Tests only | Redis server for the Redis tests (see [Redis tests](#redis-tests)). Not read by the API |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Docker Compose | Credentials and database name for the `db` container |
| `API_PORT`, `POSTGRES_PORT`, `REDIS_PORT` | Docker Compose | Ports published on `127.0.0.1` |

The API settings are defined in [`app/config.py`](app/config.py). None of them has a default, so the API refuses to start when one is missing or invalid. The error names the variable but never prints its value, because a URL can contain a password. The two secrets are also hidden when the settings are printed. Under Docker Compose, `DATABASE_URL` and `REDIS_URL` are built in `docker-compose.yml` from the `POSTGRES_*` values, so the two in `.env` are only used by `make run`.

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
    jobs.py           # job names and ids, the arq queue
  types/              # content-type registry: one module per built-in type
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
docker-compose.yml    # local stack: db, redis, api
Dockerfile            # API image
Makefile              # install, fmt, lint, typecheck, test, check, run, migrate, up, down
docs/
  ARCHITECTURE.md     # the design (source of truth)
  decisions/          # decision records
```

The full target layout is in [`docs/ARCHITECTURE.md` §6](docs/ARCHITECTURE.md#6-repository-layout). Folders are added when a ticket needs them.
