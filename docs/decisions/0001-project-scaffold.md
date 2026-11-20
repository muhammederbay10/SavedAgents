# 0001: Project scaffold choices

- Status: partly accepted. The choices under [Decision](#decision) are made inside SAV-4's scope and take effect when its pull request is merged. The items under [Pending owner decisions](#pending-owner-decisions) need Muhammed's explicit approval and are **not approved yet**.
- Date: 2026-11-16
- Ticket: SAV-4

## Context

SAV-4 creates the skeleton that every later ticket builds on. `docs/ARCHITECTURE.md` §5 fixes the stack (Python 3.12, FastAPI, Postgres 16 with PostGIS and pgvector, Redis, Docker Compose). It leaves a few practical choices open. `AGENTS.md` requires owner approval for every new dependency, so each choice below uses what the ticket already names where that is possible.

## Decision

1. **Structured logging uses the standard library.** `app/logging_config.py` installs one JSON handler on the root logger and routes uvicorn's server loggers through it. Fields are passed with `extra=`. `LOG_LEVEL` is enforced on the handler, so it also holds for libraries that set a level on their own loggers.
2. **Postgres runs from our own small image.** `docker/postgres/Dockerfile` starts from the official `postgres:16-bookworm` image and installs `postgresql-16-postgis-3` and `postgresql-16-pgvector` from the PostgreSQL apt repository. Both extensions are enabled when the data volume is first created.
3. **Settings have no defaults.** Every field in `app/config/` must be set in the environment (or in `.env` locally). A missing variable stops the app at startup.
4. **The app is built by a factory.** `create_app()` in `app/main.py` takes optional settings. Uvicorn runs it with `--factory`, and tests pass their own settings.
5. **Packaging is plain `pyproject.toml` with setuptools and pip.** Development tools are the `dev` extra. There is no lock file yet.
6. **`GET /health` is a liveness check.** It reports that the process serves requests and does not contact Postgres or Redis.
7. **Request data stays out of the logs.** Uvicorn's access log is switched off in `app/logging_config.py`, because every access record holds the client's address and the full request line with its query string.
8. **The WebSocket protocol is switched off** with `--ws none` in the image's start command and in `make run`. The design has no WebSocket endpoint, and with the protocol on, uvicorn logs the client address and query string of every WebSocket handshake on its server logger.
9. **Settings errors never repeat the rejected value.** `Settings` sets `hide_input_in_errors`, so a malformed `DATABASE_URL` or `REDIS_URL` is reported by field name without printing the URL and its password.

## Pending owner decisions

These are in the code so that the scaffold runs and can be reviewed, but Muhammed has not approved them. `AGENTS.md` §1 and SAV-22 §6 reserve them for him.

| # | Decision | Why it is there | If it is not approved |
|---|---|---|---|
| P1 | Runtime dependency `uvicorn[standard]` | FastAPI needs an ASGI server to run. The ticket and SAV-22 §6 name FastAPI but no server. | Another server is still a new dependency. The smaller option is plain `uvicorn` without the `standard` extra (no `uvloop`, `httptools`, `watchfiles` or `websockets`). |
| P2 | Development dependency `httpx` | The in-process API tests (`tests/test_health.py`) use its ASGI transport. | Those tests move to the real-process helper in `tests/api_process.py`, which uses only the standard library. |
| P3 | Git author of the SAV-4 commits | The managed checkout had an empty Git name and email. The commits use the name and GitHub noreply address of the SAV-3 commit. | The commits are re-authored before the push. That changes their SHAs, so the branch is reviewed again. |

## Alternatives

- **`structlog` for logging.** Nicer API (bound context, processors), but it is a new dependency. The standard library covers what we need today. Switching later only touches `app/logging_config.py` and the call sites' `extra=` arguments.
- **A ready-made database image** (`postgis/postgis` or `pgvector/pgvector`). Each ships only one of the two extensions, so we would still have to install the other one. The official image and the PostgreSQL apt packages are published for both Intel and ARM machines.
- **Defaults for settings.** Convenient, but the ticket asks for no hard-coded values, and a silent default for a URL is a common cause of "works on my machine".
- **Cleaning access records instead of dropping them.** A filter could keep the method, path and status. It would depend on the layout of uvicorn's internal log records. A request log of our own (method, route and status only) can be added when a ticket needs one.
- **A module-level `app` object.** It would read the environment at import time, which makes tests depend on the developer's shell and `.env`.
- **`uv` or Poetry with a lock file.** Reproducible installs, but another tool every developer and agent must install. Worth revisiting when CI is set up.
- **A health check that pings Postgres and Redis.** It needs the database and Redis clients, which arrive with later tickets. A separate readiness check can be added then.

## Consequences

- The dependencies are the ones the ticket names (FastAPI, Pydantic Settings, ruff, mypy, pytest, pytest-asyncio) plus the two pending ones, P1 and P2.
- There are no per-request log lines. A request is only visible in the logs when the application code logs something about it.
- A ticket that adds a WebSocket endpoint must remove `--ws none` and decide what the handshake may log.
- Tests start a real API process with the options from the `Dockerfile` start command, so removing `--ws none` there fails the suite.
- Dependency versions are lower bounds, so two installs on different days can differ. A lock file would fix this.
- The first `docker compose up` builds the database image, so it is slower than later starts.
- The extension script only runs on a new data volume. Migrations should still use `CREATE EXTENSION IF NOT EXISTS`.
- `/health` returning 200 does not prove the database is reachable.
