"""Shared fixtures. Tests never read the developer's `.env` or call external services."""

import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import PostgresDsn, RedisDsn

from app.config import Settings
from app.main import create_app

TEST_DATABASE_URL = "postgresql+asyncpg://test_user:test_password@localhost:5432/test_db"
TEST_REDIS_URL = "redis://localhost:6379/0"


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
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
