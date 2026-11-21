"""The deletion deadline holds while the eraser waits for the user's row lock.

Ingestion and indexing lock a user's row while they work (`lock_user`), and
the eraser waits for that lock. A wait that crosses the deadline must erase
nothing. Holding a row lock from another connection only works on committed
rows, so these tests commit, and erase what they created afterwards. They need
Postgres with PostGIS and pgvector (`TEST_DATABASE_ADMIN_URL`) and Redis
(`TEST_REDIS_URL`). Every id, text and file is synthetic.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
from arq.connections import ArqRedis
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.deletion_requests import RedisDeletionRequests
from app.db.deletion import UserDataEraser
from app.db.models import SavedItem
from app.ingestion.jobs import ArqJobQueue
from app.ingestion.repository import lock_user
from app.ingestion.session_state import RedisIngestionState
from app.pipeline.media.storage import LocalMediaStore
from tests.agent.agent_support import CONFIRM_SECONDS, FakeClock, final_answer, tool_results
from tests.agent.test_data_deletion import (
    CONFIRM,
    LEAVING_SENDER_ID,
    REQUEST,
    SavedUser,
    Stores,
    answer,
    clean_up,
    queued_job_users,
    request_deletion,
    save_something,
    user_exists,
)
from tests.redis_inspection import all_stored_bytes

# How long the test waits for the eraser to block on the lock, polling Postgres.
_BLOCKED_TIMEOUT_SECONDS: Final = 10.0
_POLL_SECONDS: Final = 0.05
_WAITING_FOR_A_LOCK = text("SELECT count(*) FROM pg_locks WHERE NOT granted")


@pytest.fixture
async def committed_engine(migrated_database_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def committed_stores(
    committed_engine: AsyncEngine,
    ingestion_redis: ArqRedis,
    ingestion_state: RedisIngestionState,
    tmp_path: Path,
) -> Stores:
    return Stores(
        sessions=async_sessionmaker(committed_engine, expire_on_commit=False),
        redis=ingestion_redis,
        state=ingestion_state,
        jobs=ArqJobQueue(ingestion_redis),
        media=LocalMediaStore(tmp_path / "media"),
        media_root=tmp_path / "media",
        requests=RedisDeletionRequests(ingestion_redis),
        clock=FakeClock(),
    )


async def hold_lock_until(
    engine: AsyncEngine, user_id: uuid.UUID, locked: asyncio.Event, release: asyncio.Event
) -> None:
    """Hold the user's row lock the way ingestion does, until the test releases it."""
    sessions = async_sessionmaker(engine)
    async with sessions() as session, session.begin():
        assert await lock_user(session, user_id)
        locked.set()
        await release.wait()


async def wait_until_blocked(engine: AsyncEngine) -> None:
    """Wait until some connection is waiting for a lock: the eraser, here."""
    async with asyncio.timeout(_BLOCKED_TIMEOUT_SECONDS):
        while True:
            async with engine.connect() as connection:
                if await connection.scalar(_WAITING_FOR_A_LOCK):
                    return
            await asyncio.sleep(_POLL_SECONDS)


async def erase_left_over(stores: Stores, user: SavedUser) -> None:
    """Remove the committed rows and every other store of the test's user."""
    await clean_up(stores, user)
    await UserDataEraser(stores.sessions, stores.outside_postgres()).erase(user.user_id)


async def test_a_lock_wait_that_crosses_the_deadline_erases_nothing(
    committed_stores: Stores, committed_engine: AsyncEngine, redis_url: str
) -> None:
    stores = committed_stores
    user = await save_something(stores, LEAVING_SENDER_ID)
    agent = stores.agent(
        [REQUEST, final_answer("en", "…"), CONFIRM, final_answer("en", "It expired.")]
    )
    locked, release = asyncio.Event(), asyncio.Event()

    try:
        await request_deletion(agent, user, "delete my data")
        # The tap arrives one second before the deadline, while ingestion holds the lock.
        stores.clock.advance(CONFIRM_SECONDS - 1)
        holder = asyncio.create_task(
            hold_lock_until(committed_engine, user.user_id, locked, release)
        )
        await locked.wait()
        tapped = asyncio.create_task(answer(agent, user, "Yes, delete", confirmed=True))
        await wait_until_blocked(committed_engine)
        # The deadline passes while the eraser waits; then the lock is released.
        stores.clock.advance(2)
        release.set()
        await holder
        result = await tapped

        assert await user_exists(stores, user.user_id)
        assert result.deleted_recipients == ()
        (refused,) = tool_results(agent.generator.requests[3])
        assert "Nothing was deleted" in str(refused["error"])
        async with stores.sessions() as session:
            items = await session.scalar(
                select(func.count()).select_from(SavedItem).where(SavedItem.user_id == user.user_id)
            )
        assert items == 1
        assert user.marker.encode() in await all_stored_bytes(redis_url)
        assert str(user.user_id) in await queued_job_users(stores.redis)
        assert (stores.media_root / str(user.user_id) / str(user.item_id)).exists()
    finally:
        release.set()
        await erase_left_over(stores, user)


async def test_a_lock_released_before_the_deadline_still_erases(
    committed_stores: Stores, committed_engine: AsyncEngine
) -> None:
    stores = committed_stores
    user = await save_something(stores, LEAVING_SENDER_ID)
    agent = stores.agent([REQUEST, final_answer("en", "…"), CONFIRM])
    locked, release = asyncio.Event(), asyncio.Event()

    try:
        await request_deletion(agent, user, "delete my data")
        stores.clock.advance(CONFIRM_SECONDS - 2)
        holder = asyncio.create_task(
            hold_lock_until(committed_engine, user.user_id, locked, release)
        )
        await locked.wait()
        tapped = asyncio.create_task(answer(agent, user, "Yes, delete", confirmed=True))
        await wait_until_blocked(committed_engine)
        stores.clock.advance(1)
        release.set()
        await holder
        result = await tapped

        assert result.deleted_recipients != ()
        assert not await user_exists(stores, user.user_id)
        assert not (stores.media_root / str(user.user_id)).exists()
    finally:
        release.set()
        await erase_left_over(stores, user)
