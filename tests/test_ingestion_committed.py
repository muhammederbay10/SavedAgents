"""Ingestion on the API's own engine: real commits, overlapping requests and log privacy.

The other ingestion tests share one connection and roll everything back. These
use the engine and session factory the API creates, on a database of their own,
so several requests can really run at the same moment.

These tests need Postgres with PostGIS and pgvector. See `tests/database.py`.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import URL, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import CaptureEvent
from app.api.webhooks import create_instagram_webhook_router
from app.config import Settings
from app.db.models import PlatformAccount, SavedItem, User
from app.db.session import create_engine, create_session_factory
from app.ingestion.service import IngestionOutcome, IngestionService
from tests.database import run_alembic
from tests.db_factories import INSTAGRAM, MESSAGE_ID, REEL_ID, REEL_URL, SENDER_ID, count_rows
from tests.webhooks import (
    HINT_AFTER_REEL,
    REEL_BEFORE_HINT,
    TEXT_QUESTION,
    RecordingJobQueue,
    fixture_body,
    fixture_message,
    signed_headers,
)

WEBHOOK_PATH = "/webhooks/instagram"
OVERLAPPING_REQUESTS = 8
HINT_WINDOW = timedelta(seconds=30)
SHARED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
PRIVATE_CAPTION = "synthetic private caption 3d9a"


@pytest.fixture
async def session_factory(
    empty_database_url: URL, tmp_path: Path
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The API's session factory on a migrated database of this test's own."""
    run_alembic(empty_database_url, tmp_path, "upgrade", "head")
    engine = create_engine(empty_database_url.render_as_string(hide_password=False))
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def job_queue() -> RecordingJobQueue:
    return RecordingJobQueue()


@pytest.fixture
def ingestion(
    session_factory: async_sessionmaker[AsyncSession], job_queue: RecordingJobQueue
) -> IngestionService:
    return IngestionService(session_factory, job_queue, HINT_WINDOW)


def reel_share(user_id: uuid.UUID, message_id: str = MESSAGE_ID) -> CaptureEvent:
    return CaptureEvent(
        user_id=user_id,
        platform=INSTAGRAM,
        source="dm",
        post_url=REEL_URL,
        platform_post_id=REEL_ID,
        caption=PRIVATE_CAPTION,
        media_url=None,
        media_type="reel",
        collection_hint=None,
        source_message_id=message_id,
        received_at=SHARED_AT,
    )


async def count(session_factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with session_factory() as session:
        return await count_rows(session, model)


async def test_overlapping_first_messages_from_one_account_create_one_user(
    ingestion: IngestionService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_ids = await asyncio.gather(
        *(ingestion.resolve_user(INSTAGRAM, SENDER_ID) for _ in range(OVERLAPPING_REQUESTS))
    )

    assert len(set(user_ids)) == 1
    assert await count(session_factory, User) == 1
    assert await count(session_factory, PlatformAccount) == 1


async def test_overlapping_deliveries_of_one_share_create_one_saved_item(
    ingestion: IngestionService,
    session_factory: async_sessionmaker[AsyncSession],
    job_queue: RecordingJobQueue,
) -> None:
    user_id = await ingestion.resolve_user(INSTAGRAM, SENDER_ID)

    outcomes = await asyncio.gather(
        *(ingestion.ingest_capture(reel_share(user_id)) for _ in range(OVERLAPPING_REQUESTS))
    )

    assert outcomes.count(IngestionOutcome.ITEM_SAVED) == 1
    assert outcomes.count(IngestionOutcome.ITEM_ALREADY_SAVED) == OVERLAPPING_REQUESTS - 1
    assert await count(session_factory, SavedItem) == 1
    assert len(job_queue.jobs) == 1


async def test_overlapping_shares_of_one_post_in_different_messages_create_one_saved_item(
    ingestion: IngestionService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id = await ingestion.resolve_user(INSTAGRAM, SENDER_ID)
    shares = [
        reel_share(user_id, message_id=f"{MESSAGE_ID}{number}")
        for number in range(OVERLAPPING_REQUESTS)
    ]

    outcomes = await asyncio.gather(*(ingestion.ingest_capture(share) for share in shares))

    assert outcomes.count(IngestionOutcome.ITEM_SAVED) == 1
    assert await count(session_factory, SavedItem) == 1


async def test_database_error_does_not_repeat_the_values_of_the_statement(
    session_factory: async_sessionmaker[AsyncSession], ingestion: IngestionService
) -> None:
    user_id = await ingestion.resolve_user(INSTAGRAM, SENDER_ID)
    # No post id, message id or post link: the database refuses the row.
    refused_item = SavedItem(user_id=user_id, platform=INSTAGRAM, caption=PRIVATE_CAPTION)

    with pytest.raises(IntegrityError) as error:
        async with session_factory.begin() as session:
            session.add(refused_item)

    assert PRIVATE_CAPTION not in str(error.value)


async def test_logs_hold_no_message_content_or_platform_ids_even_at_debug_level(
    settings: Settings,
    ingestion: IngestionService,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.include_router(create_instagram_webhook_router(settings, ingestion))
    private_values = [
        SENDER_ID,
        fixture_message(REEL_BEFORE_HINT)["mid"],
        fixture_message(HINT_AFTER_REEL)["mid"],
        "FAKEREEL006",
        "Top 5 sci-fi movies to see",
        "For my movies collection",
        "Restaurants in beşiktaş",
    ]

    with caplog.at_level(logging.DEBUG):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as webhooks:
            for name in (REEL_BEFORE_HINT, HINT_AFTER_REEL, TEXT_QUESTION):
                body = fixture_body(name)
                response = await webhooks.post(
                    WEBHOOK_PATH, content=body, headers=signed_headers(body)
                )
                assert response.status_code == 200

    async with session_factory() as session:
        item = (await session.execute(select(SavedItem))).scalar_one()
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert item.collection == "For my movies collection"
    assert "capture_ingested" in logged
    assert "chat_message_ingested" in logged
    assert [value for value in private_values if value in logged] == []
