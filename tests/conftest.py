"""Shared fixtures. Tests never read the developer's `.env` or call external services."""

import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import PostgresDsn, RedisDsn, SecretStr
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.main import create_app
from tests.database import (
    TEST_EMBEDDING_DIMENSION,
    admin_url_or_skip,
    create_database,
    drop_database,
    run_alembic,
)

TEST_DATABASE_URL = "postgresql+asyncpg://test_user:test_password@localhost:5432/test_db"
TEST_REDIS_URL = "redis://localhost:6379/0"
# Synthetic values. The real ones only ever live in `.env`.
TEST_META_APP_SECRET = "synthetic-app-secret-7c1e"
TEST_META_WEBHOOK_VERIFY_TOKEN = "synthetic-verify-token-52ab"
TEST_COLLECTION_HINT_WINDOW_SECONDS = 30


LOGGERS_CHANGED_BY_LOGGING_SETUP = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Undo `configure_logging` and uvicorn's own setup after each test."""
    loggers = [logging.getLogger(name) for name in LOGGERS_CHANGED_BY_LOGGING_SETUP]
    saved = [(list(logger.handlers), logger.level, logger.propagate) for logger in loggers]
    yield
    for logger, (handlers, level, propagate) in zip(loggers, saved, strict=True):
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture
def settings() -> Settings:
    return Settings(
        log_level="INFO",
        database_url=PostgresDsn(TEST_DATABASE_URL),
        redis_url=RedisDsn(TEST_REDIS_URL),
        embedding_dimension=TEST_EMBEDDING_DIMENSION,
        meta_app_secret=SecretStr(TEST_META_APP_SECRET),
        meta_webhook_verify_token=SecretStr(TEST_META_WEBHOOK_VERIFY_TOKEN),
        collection_hint_window_seconds=TEST_COLLECTION_HINT_WINDOW_SECONDS,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture(scope="session")
def database_admin_url() -> URL:
    """URL of the Postgres server that test databases are created on."""
    return admin_url_or_skip()


@pytest.fixture
def empty_database_url(database_admin_url: URL) -> Iterator[URL]:
    """A new, empty database, dropped after the test."""
    database_url = create_database(database_admin_url)
    try:
        yield database_url
    finally:
        drop_database(database_admin_url, database_url)


@pytest.fixture(scope="session")
def migrated_database_url(
    database_admin_url: URL, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[URL]:
    """A database with every migration applied, shared by the whole test run."""
    database_url = create_database(database_admin_url)
    try:
        run_alembic(database_url, tmp_path_factory.mktemp("alembic"), "upgrade", "head")
        yield database_url
    finally:
        drop_database(database_admin_url, database_url)


@pytest.fixture
async def db_session(migrated_database_url: URL) -> AsyncIterator[AsyncSession]:
    """A session on the migrated database. Everything a test writes is rolled back."""
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(
                bind=connection,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            ) as session:
                yield session
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(
    migrated_database_url: URL,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory on the migrated database, for code that commits by itself.

    Every session uses one connection inside one outer transaction. A commit
    only releases a savepoint, and everything is rolled back after the test.
    """
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            yield async_sessionmaker(
                bind=connection,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )
            await transaction.rollback()
    finally:
        await engine.dispose()
