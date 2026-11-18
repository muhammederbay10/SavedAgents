"""Helpers for tests that need a real Postgres with PostGIS and pgvector.

The tests never touch an existing database. Each one works in a throwaway
database that is created on the server named by `TEST_DATABASE_ADMIN_URL` and
dropped afterwards. Without that variable the database tests are skipped.

Alembic runs as a real `alembic` process, started in an empty directory with
only the environment passed in, so it never reads a developer's `.env`.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import URL, Row, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_CONFIG_PATH = REPOSITORY_ROOT / "alembic.ini"
ADMIN_URL_VARIABLE = "TEST_DATABASE_ADMIN_URL"
TEST_DATABASE_NAME_PREFIX = "savely_test_"
# Small on purpose: test vectors stay readable, and it proves the size is not hard-coded.
TEST_EMBEDDING_DIMENSION = 8
ALEMBIC_TIMEOUT_SECONDS = 60.0
# The settings require these, but migrations never use them.
UNUSED_REDIS_URL = "redis://localhost:6379/0"
UNUSED_WEBHOOK_SETTINGS = {
    "META_APP_SECRET": "unused-by-migrations",
    "META_WEBHOOK_VERIFY_TOKEN": "unused-by-migrations",
    "COLLECTION_HINT_WINDOW_SECONDS": "30",
    "INGESTION_STATE_TTL_SECONDS": "30",
}


def admin_url_or_skip() -> URL:
    """Return the URL of the server to create test databases on, or skip the test."""
    admin_url = os.environ.get(ADMIN_URL_VARIABLE)
    if not admin_url:
        pytest.skip(f"{ADMIN_URL_VARIABLE} is not set, so tests that need Postgres are skipped")
    return make_url(admin_url)


async def _execute_outside_transaction(url: URL, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


def create_database(admin_url: URL) -> URL:
    """Create an empty database with a unique name and return its URL."""
    name = f"{TEST_DATABASE_NAME_PREFIX}{uuid.uuid4().hex}"
    asyncio.run(_execute_outside_transaction(admin_url, f'CREATE DATABASE "{name}"'))
    return admin_url.set(database=name)


def drop_database(admin_url: URL, database_url: URL) -> None:
    statement = f'DROP DATABASE "{database_url.database}" WITH (FORCE)'
    asyncio.run(_execute_outside_transaction(admin_url, statement))


def run_alembic(
    database_url: URL,
    working_directory: Path,
    *arguments: str,
    embedding_dimension: int = TEST_EMBEDDING_DIMENSION,
) -> subprocess.CompletedProcess[str]:
    """Run one Alembic command against a database and fail the test if it fails."""
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "--config", str(ALEMBIC_CONFIG_PATH), *arguments],
        env={
            "LOG_LEVEL": "WARNING",
            "DATABASE_URL": database_url.render_as_string(hide_password=False),
            "REDIS_URL": UNUSED_REDIS_URL,
            "EMBEDDING_DIMENSION": str(embedding_dimension),
            **UNUSED_WEBHOOK_SETTINGS,
        },
        cwd=working_directory,
        capture_output=True,
        text=True,
        timeout=ALEMBIC_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, f"alembic {' '.join(arguments)} failed:\n{completed.stderr}"
    return completed


async def _fetch_all(database_url: URL, statement: str) -> Sequence[Row[tuple[object, ...]]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement))).all()
    finally:
        await engine.dispose()


def fetch_all(database_url: URL, statement: str) -> Sequence[Row[tuple[object, ...]]]:
    """Run one query from a synchronous test and return its rows."""
    return asyncio.run(_fetch_all(database_url, statement))
