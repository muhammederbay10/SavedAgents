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


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Undo `configure_logging` after each test so tests do not affect each other."""
    root_logger = logging.getLogger()
    handlers, level = list(root_logger.handlers), root_logger.level
    yield
    root_logger.handlers = handlers
    root_logger.setLevel(level)


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
