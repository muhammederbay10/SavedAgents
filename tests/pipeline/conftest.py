"""Fixtures for pipeline tests whose writes must be committed.

Holding the owner's row lock from another connection, as "delete my data"
does, only works on committed rows. These fixtures commit, and delete what
the test created afterwards.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from arq.connections import ArqRedis
from sqlalchemy import URL, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import User, Vocabulary
from app.ingestion.jobs import ArqJobQueue
from tests.pipeline.harness import Pipeline
from tests.pipeline.support import open_extraction, structured_reply, transcript


@pytest.fixture
async def committed_engine(migrated_database_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def committed_pipeline(
    committed_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Pipeline]:
    """A pipeline whose writes are committed, so another connection can lock its rows."""
    sessions = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with sessions() as session:
        started = await session.scalar(select(func.now()))
    pipeline = Pipeline(sessions=sessions, media_root=tmp_path / "media")
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[structured_reply(open_extraction(f"committed test {uuid.uuid4().hex}", ()))],
    )
    yield pipeline
    # Other tests share this database and expect no leftover users or values.
    async with sessions() as session, session.begin():
        await session.execute(delete(User).where(User.display_name == "Synthetic User"))
        await session.execute(delete(Vocabulary).where(Vocabulary.created_at >= started))


@pytest.fixture
async def job_queue(ingestion_redis: ArqRedis) -> AsyncIterator[ArqJobQueue]:
    """Ingestion's queue. The jobs a test queues, or queues again, are discarded afterwards."""
    queue = ArqJobQueue(ingestion_redis)
    queued: list[str] = []
    enqueue, requeue = queue.enqueue, queue.requeue

    async def recording_enqueue(name: str, job_id: str, arguments: dict[str, Any]) -> bool:
        queued.append(job_id)
        return await enqueue(name, job_id, arguments)

    async def recording_requeue(name: str, job_id: str, arguments: dict[str, Any]) -> bool:
        queued.append(job_id)
        return await requeue(name, job_id, arguments)

    queue.enqueue = recording_enqueue  # type: ignore[method-assign]
    queue.requeue = recording_requeue  # type: ignore[method-assign]
    yield queue
    await queue.discard(queued)


async def hold_owner_lock(
    engine: AsyncEngine, user_id: uuid.UUID, seconds: float, locked: asyncio.Event
) -> None:
    """Lock the user's row the way "delete my data" does, for a while."""
    async with engine.connect() as connection, connection.begin():
        await connection.execute(select(User.id).where(User.id == user_id).with_for_update())
        locked.set()
        await asyncio.sleep(seconds)
