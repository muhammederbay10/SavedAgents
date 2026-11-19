"""The real arq worker: separate queues, and recovery from failures at start-up and at the deadline.

These tests need Postgres with PostGIS and pgvector (`TEST_DATABASE_ADMIN_URL`)
and Redis (`TEST_REDIS_URL`). They queue jobs through ingestion's own queue and
run the worker the app runs, with fake models, in burst mode.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from arq.connections import ArqRedis
from arq.jobs import Job, JobStatus
from arq.worker import JobExecutionFailed
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import CaptureEvent
from app.config import Settings
from app.db.models import EnrichmentRun, ItemStatus
from app.ingestion.jobs import (
    ANSWER_CHAT_MESSAGE_JOB,
    CHAT_QUEUE,
    INDEX_SAVED_ITEM_JOB,
    INDEXING_QUEUE,
    ArqJobQueue,
    chat_job_id,
    index_job_id,
)
from app.models.errors import ModelUnavailableError
from app.pipeline.indexer import ItemIndexer
from app.pipeline.worker import WorkerResources, build_worker
from tests.pipeline.harness import IdleSweeper, MemoryPayloads, Pipeline, add_item, add_user, load
from tests.pipeline.support import (
    capture_event,
    open_extraction,
    pipeline_settings,
    structured_reply,
    transcript,
)

DEADLINE_SECONDS = 0.5
RECORDING_SECONDS = 2
JOB_TIMEOUT_SECONDS = 10


@pytest.fixture
def pipeline(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> Pipeline:
    pipeline = Pipeline(sessions=session_factory, media_root=tmp_path / "media")
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[structured_reply(open_extraction())],
    )
    return pipeline


@pytest.fixture
async def job_queue(ingestion_redis: ArqRedis) -> AsyncIterator[ArqJobQueue]:
    """Ingestion's queue. Jobs a test queues are discarded afterwards."""
    queue = ArqJobQueue(ingestion_redis)
    queued: list[str] = []
    original = queue.enqueue

    async def recording_enqueue(name: str, job_id: str, arguments: dict[str, Any]) -> bool:
        queued.append(job_id)
        return await original(name, job_id, arguments)

    queue.enqueue = recording_enqueue  # type: ignore[method-assign]
    yield queue
    await queue.discard(queued)


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


async def run_worker(
    settings: Settings,
    redis: ArqRedis,
    indexer: ItemIndexer,
    *,
    max_tries: int = 3,
    run_deadline_seconds: float = 30,
    job_timeout_seconds: float = 60,
) -> None:
    worker_settings = pipeline_settings(
        indexing_max_tries=max_tries,
        indexing_run_deadline_seconds=run_deadline_seconds,
        indexing_failure_recording_seconds=RECORDING_SECONDS,
        indexing_job_timeout_seconds=job_timeout_seconds,
    )
    worker = build_worker(
        settings,
        worker_settings,
        on_startup=startup_with(indexer),
        redis_pool=redis,
        burst=True,
    )
    # The worker borrows the fixture's Redis client, which the fixture closes.
    await worker.main()


async def queue_index_job(job_queue: ArqJobQueue, user_id: uuid.UUID, item_id: uuid.UUID) -> None:
    await job_queue.enqueue(
        INDEX_SAVED_ITEM_JOB,
        index_job_id(item_id),
        {"user_id": str(user_id), "saved_item_id": str(item_id)},
    )


async def result_of(redis: ArqRedis, item_id: uuid.UUID) -> object:
    return await Job(index_job_id(item_id), redis, _queue_name=INDEXING_QUEUE).result(timeout=5)


async def step_run(pipeline: Pipeline, item_id: uuid.UUID, step: str) -> EnrichmentRun | None:
    async with pipeline.sessions() as session:
        rows = await session.execute(
            select(EnrichmentRun).where(
                EnrichmentRun.saved_item_id == item_id, EnrichmentRun.step == step
            )
        )
        return rows.scalars().first()


# --- Blocker 1: chat jobs are never taken by the indexing worker ----------------


async def test_the_indexing_worker_leaves_chat_jobs_queued_for_their_own_worker(
    pipeline: Pipeline, settings: Settings, ingestion_redis: ArqRedis, job_queue: ArqJobQueue
) -> None:
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    receipt_id = uuid.uuid4()
    await queue_index_job(job_queue, user_id, item_id)
    await job_queue.enqueue(
        ANSWER_CHAT_MESSAGE_JOB,
        chat_job_id(receipt_id),
        {"user_id": str(user_id), "message_receipt_id": str(receipt_id)},
    )

    await run_worker(settings, ingestion_redis, pipeline.indexer())

    assert await result_of(ingestion_redis, item_id) == "indexed"
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.DONE
    chat_job = Job(chat_job_id(receipt_id), ingestion_redis, _queue_name=CHAT_QUEUE)
    assert await chat_job.status() is JobStatus.queued
    assert chat_job_id(receipt_id) in {
        job.job_id for job in await ingestion_redis.queued_jobs(queue_name=CHAT_QUEUE)
    }


async def test_erasing_a_user_discards_their_jobs_from_both_queues(
    ingestion_redis: ArqRedis, job_queue: ArqJobQueue
) -> None:
    user_id, item_id, receipt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await queue_index_job(job_queue, user_id, item_id)
    await job_queue.enqueue(
        ANSWER_CHAT_MESSAGE_JOB,
        chat_job_id(receipt_id),
        {"user_id": str(user_id), "message_receipt_id": str(receipt_id)},
    )

    await job_queue.discard([index_job_id(item_id), chat_job_id(receipt_id)])

    for queue_name, job_id in (
        (INDEXING_QUEUE, index_job_id(item_id)),
        (CHAT_QUEUE, chat_job_id(receipt_id)),
    ):
        queued = {job.job_id for job in await ingestion_redis.queued_jobs(queue_name=queue_name)}
        assert job_id not in queued
        assert await Job(job_id, ingestion_redis, _queue_name=queue_name).status() is (
            JobStatus.not_found
        )


# --- Blocker 2: start-up failures are retried ------------------------------------


class FlakyPayloads:
    """Fails like an unreachable Redis on the first read, then reads normally."""

    def __init__(self, inner: MemoryPayloads) -> None:
        self.inner = inner
        self.reads = 0

    async def capture(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> CaptureEvent | None:
        self.reads += 1
        if self.reads == 1:
            raise RedisConnectionError("synthetic: Redis is unreachable")
        return await self.inner.capture(user_id, saved_item_id)


class FlakySessions:
    """Fails like an unreachable database once, at the next session opened after `arm()`."""

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        self.inner = inner
        self.armed = False
        self.failures = 0

    def arm(self) -> None:
        self.armed = True

    def __call__(self) -> AsyncSession:
        if self.armed:
            self.armed = False
            self.failures += 1
            raise OperationalError("synthetic", {}, ConnectionRefusedError())
        return self.inner()


class ArmingPayloads:
    """Arms the session failure when a given try reads its payload, just before its start."""

    def __init__(self, inner: MemoryPayloads, sessions: FlakySessions, on_read: int) -> None:
        self.inner = inner
        self.sessions = sessions
        self.on_read = on_read
        self.reads = 0

    async def capture(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> CaptureEvent | None:
        self.reads += 1
        if self.reads == self.on_read:
            self.sessions.arm()
        return await self.inner.capture(user_id, saved_item_id)


async def test_a_redis_failure_while_reading_the_payload_is_retried(
    pipeline: Pipeline, settings: Settings, ingestion_redis: ArqRedis, job_queue: ArqJobQueue
) -> None:
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    payloads = FlakyPayloads(pipeline.payloads)
    await queue_index_job(job_queue, user_id, item_id)

    await run_worker(settings, ingestion_redis, pipeline.indexer(payloads=payloads))

    assert payloads.reads == 2
    assert await result_of(ingestion_redis, item_id) == "indexed"
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.DONE


async def test_a_database_failure_at_start_is_retried_and_keeps_finished_steps(
    pipeline: Pipeline, settings: Settings, ingestion_redis: ArqRedis, job_queue: ArqJobQueue
) -> None:
    outage = ModelUnavailableError("synthetic outage", operation="generate", model="fake/text")
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[outage, structured_reply(open_extraction())],
    )
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    # Try 1 fails at extract; try 2 fails to open the database at start; try 3 finishes.
    sessions = FlakySessions(pipeline.sessions)
    payloads = ArmingPayloads(pipeline.payloads, sessions, on_read=2)
    await queue_index_job(job_queue, user_id, item_id)

    await run_worker(
        settings,
        ingestion_redis,
        pipeline.indexer(sessions=sessions, payloads=payloads, max_tries=4),
        max_tries=4,
    )

    assert sessions.failures == 1
    assert payloads.reads == 3
    assert await result_of(ingestion_redis, item_id) == "indexed"
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.DONE
    assert pipeline.media_server.requests == 1
    assert pipeline.classifier.asked_keys().count("content_type") == 1
    assert len(pipeline.primary_stt.calls) == 1


async def test_a_start_that_keeps_failing_leaves_the_item_unfinished_for_the_sweeper(
    pipeline: Pipeline, settings: Settings, ingestion_redis: ArqRedis, job_queue: ArqJobQueue
) -> None:
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))

    class BrokenPayloads:
        async def capture(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> None:
            raise RedisConnectionError("synthetic: Redis is unreachable")

    await queue_index_job(job_queue, user_id, item_id)

    await run_worker(
        settings, ingestion_redis, pipeline.indexer(payloads=BrokenPayloads()), max_tries=2
    )

    # arq gave up on the job; no run started, so no try was spent. The item is
    # still unfinished in Postgres, where the stuck-item sweeper finds it.
    with pytest.raises(JobExecutionFailed, match="retries exceeded"):
        await result_of(ingestion_redis, item_id)
    item = await load(pipeline.sessions, item_id)
    assert item.status in {ItemStatus.PENDING, ItemStatus.ENRICHING}
    assert item.indexing_attempts == 0


# --- Blocker 3: the deadline is handled inside the job ---------------------------


async def test_a_run_past_its_deadline_records_the_step_and_resumes_on_the_next_try(
    pipeline: Pipeline,
    settings: Settings,
    ingestion_redis: ArqRedis,
    job_queue: ArqJobQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pipeline.classifier.stalled_calls = 1
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    await queue_index_job(job_queue, user_id, item_id)
    caplog.set_level(logging.INFO, logger="app.pipeline")

    await run_worker(
        settings,
        ingestion_redis,
        pipeline.indexer(run_deadline_seconds=DEADLINE_SECONDS),
        run_deadline_seconds=DEADLINE_SECONDS,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
    )

    assert await result_of(ingestion_redis, item_id) == "indexed"
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.DONE
    retry = next(
        record for record in caplog.records if record.message == "saved_item_indexing_retry"
    )
    assert retry.step == "classify"
    assert "IndexingDeadlineError" in retry.error
    assert pipeline.media_server.requests == 1
    assert pipeline.classifier.asked_keys().count("content_type") == 2


async def test_a_run_past_its_deadline_on_the_last_try_marks_the_item_failed(
    pipeline: Pipeline, settings: Settings, ingestion_redis: ArqRedis, job_queue: ArqJobQueue
) -> None:
    pipeline.classifier.stalled_calls = 2
    user_id = await add_user(pipeline.sessions)
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    await queue_index_job(job_queue, user_id, item_id)

    await run_worker(
        settings,
        ingestion_redis,
        pipeline.indexer(run_deadline_seconds=DEADLINE_SECONDS, max_tries=2),
        max_tries=2,
        run_deadline_seconds=DEADLINE_SECONDS,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
    )

    assert await result_of(ingestion_redis, item_id) == "failed"
    assert (await load(pipeline.sessions, item_id)).status == ItemStatus.FAILED
    classify = await step_run(pipeline, item_id, "classify")
    assert classify is not None
    assert classify.status == "failed"
    assert "IndexingDeadlineError" in str(classify.error)
    fetch = await step_run(pipeline, item_id, "fetch")
    assert fetch is not None and fetch.status == "done"
