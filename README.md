# SavedAgent

SavedAgent indexes posts you save on Instagram (later TikTok, X and Facebook) and lets you search them by chat: places by district, movies by genre or streaming platform, study material by subject, and more.

- Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Rules for contributors and agents: [`AGENTS.md`](AGENTS.md)
- Decision records: [`docs/decisions/`](docs/decisions/)

This repository currently holds the project skeleton (a FastAPI app with a health endpoint, a local Docker stack and the development tooling) and the content-type registry.

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

The values in `.env.example` work as they are for local development.

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

Tests never call external services and do not need Docker.

## Configuration

All configuration comes from environment variables. Locally they live in `.env`, and `.env.example` lists every variable.

| Variable | Used by | Meaning |
|---|---|---|
| `LOG_LEVEL` | API | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `DATABASE_URL` | API | Postgres URL, e.g. `postgresql+asyncpg://user:password@host:5432/db` |
| `REDIS_URL` | API | Redis URL, e.g. `redis://host:6379/0` |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Docker Compose | Credentials and database name for the `db` container |
| `API_PORT`, `POSTGRES_PORT`, `REDIS_PORT` | Docker Compose | Ports published on `127.0.0.1` |

The API settings are defined in [`app/config.py`](app/config.py). None of them has a default, so the API refuses to start when one is missing or invalid. The error names the variable but never prints its value, because a URL can contain a password. Under Docker Compose, `DATABASE_URL` and `REDIS_URL` are built in `docker-compose.yml` from the `POSTGRES_*` values, so the two in `.env` are only used by `make run`.

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
  types/              # content-type registry: one module per built-in type
tests/                # pytest suite
docker/postgres/      # Postgres 16 image with PostGIS and pgvector
docker-compose.yml    # local stack: db, redis, api
Dockerfile            # API image
Makefile              # install, fmt, lint, typecheck, test, check, run, up, down
docs/
  ARCHITECTURE.md     # the design (source of truth)
  decisions/          # decision records
```

The full target layout is in [`docs/ARCHITECTURE.md` §6](docs/ARCHITECTURE.md#6-repository-layout). Folders are added when a ticket needs them.
