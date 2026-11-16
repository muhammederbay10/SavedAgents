# 0001: Project scaffold choices

- Status: accepted
- Date: 2026-11-16
- Ticket: SAV-4

## Context

SAV-4 creates the skeleton that every later ticket builds on. `docs/ARCHITECTURE.md` §5 fixes the stack (Python 3.12, FastAPI, Postgres 16 with PostGIS and pgvector, Redis, Docker Compose). It leaves a few practical choices open. `AGENTS.md` requires owner approval for every new dependency, so each choice below uses what the ticket already names where that is possible.

## Decision

1. **Structured logging uses the standard library.** `app/logging_config.py` installs one JSON formatter on the root logger and routes uvicorn's loggers through it. Fields are passed with `extra=`.
2. **Postgres runs from our own small image.** `docker/postgres/Dockerfile` starts from the official `postgres:16-bookworm` image and installs `postgresql-16-postgis-3` and `postgresql-16-pgvector` from the PostgreSQL apt repository. Both extensions are enabled when the data volume is first created.
3. **Settings have no defaults.** Every field in `app/config.py` must be set in the environment (or in `.env` locally). A missing variable stops the app at startup.
4. **The app is built by a factory.** `create_app()` in `app/main.py` takes optional settings. Uvicorn runs it with `--factory`, and tests pass their own settings.
5. **Packaging is plain `pyproject.toml` with setuptools and pip.** Development tools are the `dev` extra. There is no lock file yet.
6. **`GET /health` is a liveness check.** It reports that the process serves requests and does not contact Postgres or Redis.

## Alternatives

- **`structlog` for logging.** Nicer API (bound context, processors), but it is a new dependency. The standard library covers what we need today. Switching later only touches `app/logging_config.py` and the call sites' `extra=` arguments.
- **A ready-made database image** (`postgis/postgis` or `pgvector/pgvector`). Each ships only one of the two extensions, so we would still have to install the other one. The official image and the PostgreSQL apt packages are published for both Intel and ARM machines.
- **Defaults for settings.** Convenient, but the ticket asks for no hard-coded values, and a silent default for a URL is a common cause of "works on my machine".
- **A module-level `app` object.** It would read the environment at import time, which makes tests depend on the developer's shell and `.env`.
- **`uv` or Poetry with a lock file.** Reproducible installs, but another tool every developer and agent must install. Worth revisiting when CI is set up.
- **A health check that pings Postgres and Redis.** It needs the database and Redis clients, which arrive with later tickets. A separate readiness check can be added then.

## Consequences

- No dependencies beyond the ones the ticket implies: FastAPI, Pydantic Settings and uvicorn at runtime, plus ruff, mypy, pytest, pytest-asyncio and httpx (for in-process API tests) for development.
- Dependency versions are lower bounds, so two installs on different days can differ. A lock file would fix this.
- The first `docker compose up` builds the database image, so it is slower than later starts.
- The extension script only runs on a new data volume. Migrations should still use `CREATE EXTENSION IF NOT EXISTS`.
- `/health` returning 200 does not prove the database is reachable.
