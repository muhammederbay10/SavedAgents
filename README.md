# SavedAgent

SavedAgent indexes posts you save on Instagram (later TikTok, X and Facebook) and lets you search them by chat: places by district, movies by genre or streaming platform, study material by subject, and more.

- Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Rules for contributors and agents: [`AGENTS.md`](AGENTS.md)
- Decision records: [`docs/decisions/`](docs/decisions/)

This repository currently holds the project skeleton: a FastAPI app with a health endpoint, a local Docker stack and the development tooling.

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

## Project layout

```
app/
  main.py             # FastAPI app factory and GET /health
  config.py           # settings from environment variables
  logging_config.py   # JSON logging setup
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
