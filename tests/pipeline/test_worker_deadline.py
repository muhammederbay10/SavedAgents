"""One deadline per run, enforced by the real arq worker, and what the sweeper repairs after it.

A run's budget (`INDEXING_RUN_DEADLINE_SECONDS`) starts before the payload is
read, so a slow start-up uses it up too. Recording a failure then has its own
allowance, and a clean-up reserve is kept free; arq's own limit is longer than
all three. What a job can't write (the owner's row stays locked), the
stuck-item sweeper repairs later from Postgres. The tests hold the owner's row
lock from another connection, as "delete my data" does.

These tests need Postgres with PostGIS and pgvector (`TEST_DATABASE_ADMIN_URL`)
and Redis (`TEST_REDIS_URL`).
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from arq.connections import ArqRedis
from arq.jobs import Job
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.base import CaptureEvent
from app.config import Settings
from app.db.models import ItemStatus, SavedItem
from app.ingestion.jobs import INDEX_SAVED_ITEM_JOB, INDEXING_QUEUE, ArqJobQueue, index_job_id
from app.models.base import GenerationRequest, GenerationResult
from app.models.errors import ModelUnavailableError
from app.models.fakes import FakeGenerator
from app.pipeline.indexer import ItemIndexer
from app.pipeline.worker import WorkerResources, build_worker
from tests.pipeline.conftest import hold_owner_lock
from tests.pipeline.harness import IdleSweeper, MemoryPayloads, Pipeline, add_item, add_user, load
from tests.pipeline.support import (
    capture_event,
    open_extraction,
    pipeline_settings,
    structured_reply,
    transcript,
)


@pytest.fixture
def pipeline(committed_pipeline: Pipeline) -> Pipeline:
    return committed_pipeline


async def no_close() -> None:
    return None


def startup_with(indexer: ItemIndexer) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def startup(ctx: dict[str, Any]) -> None:
        ctx["indexing_resources"] = WorkerResources(
            indexer=indexer,
            sweeper=IdleSweeper(),  # type: ignore[arg-type]
            retry_delay_seconds=0.01,
            close=no_close,
        )

    return startup


async def run_burst_worker(
    settings: Settings,
    redis: ArqRedis,
    indexer: ItemIndexer,
    *,
    max_tries: int,
    run_deadline_seconds: float,
    recording_seconds: float,
    cleanup_reserve_seconds: float,
    job_timeout_seconds: float,
) -> float:
    """Run the app's worker until its queue is empty; return how long that took."""
    worker_settings = pipeline_settings(
        indexing_max_tries=max_tries,
        indexing_run_deadline_seconds=run_deadline_seconds,
        indexing_failure_recording_seconds=recording_seconds,
        indexing_cleanup_reserve_seconds=cleanup_reserve_seconds,
        # The reserve must cover stopping a media tool twice over.
        media_tool_stop_timeout_seconds=cleanup_reserve_seconds / 2,
        indexing_job_timeout_seconds=job_timeout_seconds,
    )
    worker = build_worker(
        settings, worker_settings, on_startup=startup_with(indexer), redis_pool=redis, burst=True
    )
    started = time.monotonic()
    # The worker borrows the fixture's Redis client, which the fixture closes.
    await worker.main()
    return time.monotonic() - started


async def queue_index_job(job_queue: ArqJobQueue, user_id: uuid.UUID, item_id: uuid.UUID) -> None:
    await job_queue.enqueue(
        INDEX_SAVED_ITEM_JOB,
        index_job_id(item_id),
        {"user_id": str(user_id), "saved_item_id": str(item_id)},
    )


async def result_of(redis: ArqRedis, item_id: uuid.UUID) -> object:
    return await Job(index_job_id(item_id), redis, _queue_name=INDEXING_QUEUE).result(timeout=5)


async def age(pipeline: Pipeline, item_id: uuid.UUID, by: timedelta) -> None:
    """Make it look as if no job or sweep has worked on the item for a while."""
    async with pipeline.sessions() as session, session.begin():
        await session.execute(
            update(SavedItem)
            .where(SavedItem.id == item_id)
            .values(indexing_updated_at=func.now() - by)
        )


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
    cleanup_reserve_seconds: float,
    job_timeout_seconds: float,
    max_tries: int,
) -> tuple[uuid.UUID, object]:
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    locked = asyncio.Event()
    lock = asyncio.create_task(hold_owner_lock(engine, user_id, lock_seconds, locked))
    await locked.wait()
    await queue_index_job(job_queue, user_id, item_id)
    indexer = pipeline.indexer(
        run_deadline_seconds=run_deadline_seconds,
        failure_recording_seconds=recording_seconds,
        max_tries=max_tries,
    )
    try:
        await run_burst_worker(
            settings,
            redis,
            indexer,
            max_tries=max_tries,
            run_deadline_seconds=run_deadline_seconds,
            recording_seconds=recording_seconds,
            cleanup_reserve_seconds=cleanup_reserve_seconds,
            job_timeout_seconds=job_timeout_seconds,
        )
    finally:
        await lock
    return item_id, await result_of(redis, item_id)


async def test_a_start_stalled_by_the_owners_lock_is_retried_before_arq_cancels_it(
    pipeline: Pipeline,
    committed_engine: AsyncEngine,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
) -> None:
    # The lock outlasts arq's limit. Each run gives up at its deadline and runs
    # again, until the lock is gone and a run gets through.
    item_id, result = await index_with_lock(
        pipeline,
        committed_engine,
        settings,
        ingestion_redis,
        job_queue,
        lock_seconds=2.0,
        run_deadline_seconds=0.4,
        recording_seconds=0.3,
        cleanup_reserve_seconds=0.2,
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

    item_id, result = await index_with_lock(
        pipeline,
        committed_engine,
        settings,
        ingestion_redis,
        job_queue,
        lock_seconds=0.3,
        run_deadline_seconds=0.8,
        recording_seconds=0.1,
        cleanup_reserve_seconds=0.1,
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


# --- What a job couldn't write, a later sweep repairs ---------------------------


class LockingTextModel:
    """The text model, but the owner's row gets locked while it answers with an outage."""

    def __init__(self, inner: FakeGenerator, engine: AsyncEngine, lock_seconds: float) -> None:
        self.inner = inner
        self.engine = engine
        self.lock_seconds = lock_seconds
        self.user_id: uuid.UUID | None = None
        self.lock: asyncio.Task[None] | None = None

    @property
    def requests(self) -> list[GenerationRequest]:
        return self.inner.requests

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if self.lock is None and self.user_id is not None:
            locked = asyncio.Event()
            self.lock = asyncio.create_task(
                hold_owner_lock(self.engine, self.user_id, self.lock_seconds, locked)
            )
            await locked.wait()
        return await self.inner.generate(request)


async def test_a_last_try_that_cannot_write_failed_is_repaired_by_a_later_sweep(
    pipeline: Pipeline,
    committed_engine: AsyncEngine,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The item's only try fails at extract, and the owner's row is locked from
    # then on, so the job can't write `failed`. It gives up in time and ends.
    outage = ModelUnavailableError("synthetic outage", operation="generate", model="fake/text")
    text_model = LockingTextModel(FakeGenerator([outage]), committed_engine, lock_seconds=3.0)
    pipeline.text_model = text_model  # type: ignore[assignment]
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    text_model.user_id = user_id
    await queue_index_job(job_queue, user_id, item_id)
    caplog.set_level("INFO", logger="app.pipeline")

    elapsed = await run_burst_worker(
        settings,
        ingestion_redis,
        pipeline.indexer(run_deadline_seconds=1.5, failure_recording_seconds=0.3, max_tries=1),
        max_tries=1,
        run_deadline_seconds=1.5,
        recording_seconds=0.3,
        cleanup_reserve_seconds=0.5,
        job_timeout_seconds=3.0,
    )

    assert await result_of(ingestion_redis, item_id) == "failed"
    # The job ended itself, while the owner's row was still locked.
    assert elapsed < 3.0
    assert any(record.message == "indexing_failure_not_recorded" for record in caplog.records)
    stranded = await load(pipeline.sessions, item_id)
    assert (stranded.status, stranded.indexing_attempts) == (ItemStatus.ENRICHING, 1)

    assert text_model.lock is not None
    await text_model.lock
    await age(pipeline, item_id, timedelta(hours=1))
    report = await pipeline.sweeper(job_queue, max_tries=1).sweep()

    assert report.failed == 1
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.FAILED
    assert pipeline.media_server.requests == 1
    assert pipeline.classifier.asked_keys().count("content_type") == 1
    assert len(pipeline.primary_stt.calls) == 1
    assert len(text_model.requests) == 1


class LockingPayloads:
    """On the given read, has another connection lock the owner's row before going on."""

    def __init__(
        self, inner: MemoryPayloads, engine: AsyncEngine, *, on_read: int, lock_seconds: float
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


async def test_a_job_arq_gave_up_on_is_requeued_by_a_later_sweep_and_resumes(
    pipeline: Pipeline,
    committed_engine: AsyncEngine,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
) -> None:
    # Try 1 fails at extract. Try 2 can't even start (the owner's row is locked),
    # so arq runs out of tries with the item still enriching and a try left.
    outage = ModelUnavailableError("synthetic outage", operation="generate", model="fake/text")
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[outage, structured_reply(open_extraction())],
    )
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    payloads = LockingPayloads(pipeline.payloads, committed_engine, on_read=2, lock_seconds=1.5)
    budget: dict[str, Any] = {
        "max_tries": 2,
        "run_deadline_seconds": 0.4,
        "recording_seconds": 0.3,
        "cleanup_reserve_seconds": 0.2,
        "job_timeout_seconds": 1.0,
    }
    await queue_index_job(job_queue, user_id, item_id)

    await run_burst_worker(
        settings,
        ingestion_redis,
        pipeline.indexer(
            payloads=payloads, run_deadline_seconds=0.4, failure_recording_seconds=0.3, max_tries=2
        ),
        **budget,
    )

    stranded = await load(pipeline.sessions, item_id)
    assert (stranded.status, stranded.indexing_attempts) == (ItemStatus.ENRICHING, 1)
    assert payloads.lock is not None
    await payloads.lock
    await age(pipeline, item_id, timedelta(hours=1))
    report = await pipeline.sweeper(job_queue, max_tries=2).sweep()
    assert report.requeued == 1

    await run_burst_worker(
        settings,
        ingestion_redis,
        pipeline.indexer(run_deadline_seconds=0.4, failure_recording_seconds=0.3, max_tries=2),
        **budget,
    )

    done = await load(pipeline.sessions, item_id)
    assert (done.status, done.indexing_attempts) == (ItemStatus.DONE, 2)
    assert pipeline.media_server.requests == 1
    assert pipeline.classifier.asked_keys().count("content_type") == 1
    assert len(pipeline.primary_stt.calls) == 1
