"""One deadline per run, from before the first read, enforced by the real arq worker.

A run's budget (`INDEXING_RUN_DEADLINE_SECONDS`) starts before the payload is
read, so a slow start-up uses it up too. What is left of arq's own limit after
it (`INDEXING_JOB_TIMEOUT_SECONDS`) is for recording the failure, bounded by
`INDEXING_FAILURE_RECORDING_SECONDS`. The tests hold the owner's row lock from
another connection, as "delete my data" does, so the data they write is
committed. They delete it again afterwards.

These tests need Postgres with PostGIS and pgvector (`TEST_DATABASE_ADMIN_URL`)
and Redis (`TEST_REDIS_URL`).
"""

import asyncio
import contextlib
import itertools
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from arq.connections import ArqRedis
from arq.jobs import Job, JobStatus
from arq.worker import Worker
from sqlalchemy import URL, delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.adapters.base import CaptureEvent
from app.config import Settings
from app.db.models import ItemStatus, User, Vocabulary
from app.ingestion.erasure import IngestionDataEraser
from app.ingestion.jobs import (
    INDEX_SAVED_ITEM_JOB,
    INDEXING_QUEUE,
    ArqJobQueue,
    finalize_job_id,
    index_job_id,
)
from app.ingestion.session_state import RedisIngestionState
from app.models.errors import ModelUnavailableError
from app.pipeline.indexer import ItemIndexer
from app.pipeline.worker import ArqFinalizationScheduler, WorkerResources, build_worker
from tests.pipeline.harness import MemoryPayloads, Pipeline, add_item, add_user, load
from tests.pipeline.support import (
    capture_event,
    open_extraction,
    pipeline_settings,
    structured_reply,
    transcript,
)

FINALIZE_DEFER_SECONDS = 0.05


@pytest.fixture
async def committed_engine(migrated_database_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def pipeline(committed_engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[Pipeline]:
    """A pipeline whose writes are committed, so another connection can lock its rows."""
    sessions = async_sessionmaker(committed_engine, expire_on_commit=False)
    category = f"deadline test {uuid.uuid4().hex}"
    pipeline = Pipeline(sessions=sessions, media_root=tmp_path / "media")
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[structured_reply(open_extraction(category, ()))],
    )
    yield pipeline
    async with sessions() as session, session.begin():
        await session.execute(delete(User).where(User.display_name == "Synthetic User"))
        await session.execute(delete(Vocabulary).where(Vocabulary.value == category))


@pytest.fixture
async def job_queue(ingestion_redis: ArqRedis) -> AsyncIterator[ArqJobQueue]:
    queue = ArqJobQueue(ingestion_redis)
    queued: list[str] = []
    original = queue.enqueue

    async def recording_enqueue(
        name: str, job_id: str, arguments: dict[str, Any], *, defer_seconds: float | None = None
    ) -> bool:
        queued.append(job_id)
        return await original(name, job_id, arguments, defer_seconds=defer_seconds)

    queue.enqueue = recording_enqueue  # type: ignore[method-assign]
    yield queue
    await queue.discard(queued)


async def no_close() -> None:
    return None


def startup_with(
    indexer: ItemIndexer, max_tries: int
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def startup(ctx: dict[str, Any]) -> None:
        ctx["indexing_resources"] = WorkerResources(
            indexer=indexer, max_tries=max_tries, retry_delay_seconds=0.01, close=no_close
        )

    return startup


async def hold_owner_lock(
    engine: AsyncEngine, user_id: uuid.UUID, seconds: float, locked: asyncio.Event
) -> None:
    """Lock the user's row the way "delete my data" does, for a while."""
    async with engine.connect() as connection, connection.begin():
        await connection.execute(select(User.id).where(User.id == user_id).with_for_update())
        locked.set()
        await asyncio.sleep(seconds)


async def index_with_lock(
    pipeline: Pipeline,
    engine: AsyncEngine,
    settings: Settings,
    redis: ArqRedis,
    job_queue: ArqJobQueue,
    *,
    lock_seconds: float,
    run_deadline_seconds: float,
    recording_seconds: float,
    job_timeout_seconds: float,
    max_tries: int,
) -> tuple[uuid.UUID, object, float]:
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    locked = asyncio.Event()
    lock = asyncio.create_task(hold_owner_lock(engine, user_id, lock_seconds, locked))
    await locked.wait()
    await job_queue.enqueue(
        INDEX_SAVED_ITEM_JOB,
        index_job_id(item_id),
        {"user_id": str(user_id), "saved_item_id": str(item_id)},
    )
    worker_settings = pipeline_settings(
        indexing_max_tries=max_tries,
        indexing_run_deadline_seconds=run_deadline_seconds,
        indexing_failure_recording_seconds=recording_seconds,
        indexing_job_timeout_seconds=job_timeout_seconds,
    )
    indexer = pipeline.indexer(
        run_deadline_seconds=run_deadline_seconds,
        failure_recording_seconds=recording_seconds,
        finalization=ArqFinalizationScheduler(job_queue, defer_seconds=FINALIZE_DEFER_SECONDS),
    )
    worker = build_worker(
        settings,
        worker_settings,
        on_startup=startup_with(indexer, max_tries),
        redis_pool=redis,
        burst=True,
    )
    started = time.monotonic()
    try:
        await worker.main()
        elapsed = time.monotonic() - started
    finally:
        await lock
    result = await Job(index_job_id(item_id), redis, _queue_name=INDEXING_QUEUE).result(timeout=5)
    return item_id, result, elapsed


async def test_a_start_stalled_by_the_owners_lock_is_retried_before_arq_cancels_it(
    pipeline: Pipeline,
    committed_engine: AsyncEngine,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
) -> None:
    # The lock outlasts arq's limit. Each run gives up at its deadline and runs
    # again, until the lock is gone and a run gets through.
    item_id, result, _ = await index_with_lock(
        pipeline,
        committed_engine,
        settings,
        ingestion_redis,
        job_queue,
        lock_seconds=2.0,
        run_deadline_seconds=0.4,
        recording_seconds=0.3,
        job_timeout_seconds=1.0,
        max_tries=12,
    )

    assert result == "indexed"
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.DONE


async def test_a_delayed_start_then_a_slow_step_shares_one_deadline(
    pipeline: Pipeline,
    committed_engine: AsyncEngine,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The reviewer's case: start-up waits 0.3 s, then classify hangs. With one
    # budget, the run stops at 0.8 s, inside classify (fetch has 0.5 s to finish).
    # With a fresh budget after start-up, it would still be in classify at 1.1 s,
    # after arq cancels it at 1.05 s.
    pipeline.classifier.stalled_calls = 1
    caplog.set_level("INFO", logger="app.pipeline")

    item_id, result, _ = await index_with_lock(
        pipeline,
        committed_engine,
        settings,
        ingestion_redis,
        job_queue,
        lock_seconds=0.3,
        run_deadline_seconds=0.8,
        recording_seconds=0.2,
        job_timeout_seconds=1.05,
        max_tries=3,
    )

    assert result == "indexed"
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.DONE
    retry = next(
        record for record in caplog.records if record.message == "saved_item_indexing_retry"
    )
    assert retry.step == "classify"
    assert "IndexingDeadlineError" in retry.error
    assert pipeline.media_server.requests == 1


async def test_failure_recording_blocked_by_the_lock_is_bounded_on_the_last_try(
    pipeline: Pipeline,
    committed_engine: AsyncEngine,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="app.pipeline")
    # The lock outlasts every try. The last try can't write `failed` either, but
    # it gives up on that in time and ends the job itself, before arq would. The
    # status write is kept as its own job, which the worker runs until it works.
    item_id, result, elapsed = await index_with_lock(
        pipeline,
        committed_engine,
        settings,
        ingestion_redis,
        job_queue,
        lock_seconds=3.0,
        run_deadline_seconds=0.4,
        recording_seconds=0.3,
        job_timeout_seconds=1.0,
        max_tries=2,
    )

    assert result == "failed"
    assert any(record.message == "indexing_failure_not_recorded" for record in caplog.records)
    # The burst worker went on running the status job until the lock was gone.
    assert elapsed >= 3.0
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.FAILED


# --- The final status survives the job that could not save it -------------------


class LockingPayloads:
    """On the given read, has another connection lock the owner's row before going on."""

    def __init__(
        self,
        inner: MemoryPayloads,
        engine: AsyncEngine,
        *,
        on_read: int,
        lock_seconds: float,
    ) -> None:
        self.inner = inner
        self.engine = engine
        self.on_read = on_read
        self.lock_seconds = lock_seconds
        self.reads = 0
        self.lock: asyncio.Task[None] | None = None

    async def capture(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> CaptureEvent | None:
        self.reads += 1
        if self.reads == self.on_read:
            locked = asyncio.Event()
            self.lock = asyncio.create_task(
                hold_owner_lock(self.engine, user_id, self.lock_seconds, locked)
            )
            await locked.wait()
        return await self.inner.capture(user_id, saved_item_id)


async def wait_until_complete(redis: ArqRedis, job_id: str) -> None:
    """Wait for a job to finish, by polling Redis the way a separate observer would."""
    job = Job(job_id, redis, _queue_name=INDEXING_QUEUE)
    async with asyncio.timeout(10):
        for _ in itertools.count():
            if await job.status() is JobStatus.complete:
                return
            await asyncio.sleep(0.02)


async def test_a_failed_status_the_last_try_could_not_save_is_saved_by_a_later_worker(
    pipeline: Pipeline,
    committed_engine: AsyncEngine,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
) -> None:
    outage = ModelUnavailableError("synthetic outage", operation="generate", model="fake/text")
    pipeline.script(transcripts=[transcript("synthetic speech")], extractions=[outage])
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    # Try 1 runs fetch, classify and transcribe, then the text model fails. Try 2,
    # the last, finds the owner's row locked until well after it has ended.
    payloads = LockingPayloads(pipeline.payloads, committed_engine, on_read=2, lock_seconds=2.0)
    worker_settings = pipeline_settings(
        indexing_max_tries=2,
        indexing_run_deadline_seconds=0.4,
        indexing_failure_recording_seconds=0.3,
        indexing_job_timeout_seconds=1.0,
    )

    def worker() -> Worker:
        indexer = pipeline.indexer(
            payloads=payloads,
            run_deadline_seconds=0.4,
            failure_recording_seconds=0.3,
            # Long enough that the first worker is gone before the status job runs.
            finalization=ArqFinalizationScheduler(job_queue, defer_seconds=1.0),
        )
        return build_worker(
            settings,
            worker_settings,
            on_startup=startup_with(indexer, 2),
            redis_pool=ingestion_redis,
            burst=True,
        )

    await job_queue.enqueue(
        INDEX_SAVED_ITEM_JOB,
        index_job_id(item_id),
        {"user_id": str(user_id), "saved_item_id": str(item_id)},
    )
    first_worker = asyncio.create_task(worker().main())
    await wait_until_complete(ingestion_redis, index_job_id(item_id))
    # The worker process goes away; only what is in Redis survives it.
    first_worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first_worker

    index_job = Job(index_job_id(item_id), ingestion_redis, _queue_name=INDEXING_QUEUE)
    assert await index_job.result(timeout=1) == "failed"
    status_job = Job(finalize_job_id(item_id), ingestion_redis, _queue_name=INDEXING_QUEUE)
    assert await status_job.status() in {JobStatus.deferred, JobStatus.queued}
    # The lock kept the last try from writing `failed`: the item is stranded mid-way.
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.ENRICHING

    assert payloads.lock is not None
    await payloads.lock
    await worker().main()

    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.FAILED
    assert await status_job.status() is JobStatus.not_found
    # Nothing that had finished was run again.
    assert pipeline.media_server.requests == 1
    assert pipeline.classifier.asked_keys().count("content_type") == 1
    assert len(pipeline.primary_stt.calls) == 1
    assert len(pipeline.text_model.requests) == 1


async def test_erasing_a_user_removes_their_pending_status_job(
    ingestion_redis: ArqRedis, job_queue: ArqJobQueue
) -> None:
    user_id, item_id = uuid.uuid4(), uuid.uuid4()
    await ArqFinalizationScheduler(job_queue, defer_seconds=60).schedule(user_id, item_id)
    status_job = Job(finalize_job_id(item_id), ingestion_redis, _queue_name=INDEXING_QUEUE)
    assert await status_job.status() is JobStatus.deferred

    eraser = IngestionDataEraser(
        RedisIngestionState(ingestion_redis, timedelta(seconds=60)), job_queue
    )
    await eraser.erase_user(user_id, [item_id])

    assert await status_job.status() is JobStatus.not_found
