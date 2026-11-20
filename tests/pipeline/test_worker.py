"""The arq job: ingestion's contract, retries with a growing wait, and no content in its result."""

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from arq import Retry
from arq.connections import ArqRedis
from arq.jobs import Job

from app.config.base import Settings
from app.ingestion.jobs import INDEX_SAVED_ITEM_JOB, INDEXING_QUEUE, ArqJobQueue, index_job_id
from app.pipeline.indexer import IndexOutcome, RetryLaterError
from app.pipeline.recovery.runs import Step
from app.pipeline.recovery.sweeper import SweepReport
from app.pipeline.worker import (
    SWEEP_STUCK_ITEMS_JOB,
    WorkerResources,
    build_worker,
    index_saved_item,
    sweep_schedule,
    sweep_stuck_items,
)
from tests.pipeline.support import pipeline_settings

RETRY_DELAY_SECONDS = 2.0


@dataclass
class StubIndexer:
    """Stands in for `ItemIndexer` and records how it was called."""

    fail_with_retry: bool = False
    calls: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)

    async def index(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> IndexOutcome:
        self.calls.append((user_id, saved_item_id))
        if self.fail_with_retry:
            raise RetryLaterError(Step.EXTRACT, TimeoutError())
        return IndexOutcome.INDEXED


@dataclass
class StubSweeper:
    """Stands in for `StuckItemSweeper` and counts its sweeps."""

    sweeps: int = 0

    async def sweep(self) -> SweepReport:
        self.sweeps += 1
        return SweepReport(requeued=1, failed=2, skipped=3)


async def no_close() -> None:
    return None


def context(
    indexer: StubIndexer, job_try: int, sweeper: StubSweeper | None = None
) -> dict[str, Any]:
    resources = WorkerResources(
        indexer=indexer,  # type: ignore[arg-type]
        sweeper=sweeper or StubSweeper(),  # type: ignore[arg-type]
        retry_delay_seconds=RETRY_DELAY_SECONDS,
        close=no_close,
    )
    return {"indexing_resources": resources, "job_try": job_try}


async def test_the_job_takes_ids_as_text_and_returns_only_the_outcome() -> None:
    indexer = StubIndexer()
    user_id, item_id = uuid.uuid4(), uuid.uuid4()

    result = await index_saved_item(
        context(indexer, job_try=1), user_id=str(user_id), saved_item_id=str(item_id)
    )

    assert result == "indexed"
    assert indexer.calls == [(user_id, item_id)]


@pytest.mark.parametrize("job_try", [1, 2])
async def test_a_retryable_failure_asks_arq_to_try_again_later(job_try: int) -> None:
    indexer = StubIndexer(fail_with_retry=True)

    with pytest.raises(Retry) as raised:
        await index_saved_item(
            context(indexer, job_try), user_id=str(uuid.uuid4()), saved_item_id=str(uuid.uuid4())
        )

    assert raised.value.defer_score == RETRY_DELAY_SECONDS * job_try * 1000


async def test_the_sweep_job_runs_the_sweeper_and_returns_counts_only() -> None:
    sweeper = StubSweeper()

    result = await sweep_stuck_items(context(StubIndexer(), 1, sweeper))

    assert sweeper.sweeps == 1
    assert result == {"requeued": 1, "failed": 2, "skipped": 3}


async def test_a_job_queued_like_ingestion_queues_it_runs_on_the_worker(
    ingestion_redis: ArqRedis, settings: Settings
) -> None:
    indexer = StubIndexer()
    user_id, item_id = uuid.uuid4(), uuid.uuid4()

    async def inject(ctx: dict[str, Any]) -> None:
        ctx.update(context(indexer, job_try=0))

    job_queue = ArqJobQueue(ingestion_redis)
    await job_queue.enqueue(
        INDEX_SAVED_ITEM_JOB,
        index_job_id(item_id),
        {"user_id": str(user_id), "saved_item_id": str(item_id)},
    )
    worker = build_worker(
        settings, pipeline_settings(), on_startup=inject, redis_pool=ingestion_redis, burst=True
    )
    try:
        # The worker borrows the fixture's Redis client, which the fixture closes.
        await worker.main()

        assert (user_id, item_id) in indexer.calls
        job = Job(index_job_id(item_id), ingestion_redis, _queue_name=INDEXING_QUEUE)
        assert await job.result(timeout=5) == "indexed"
    finally:
        await job_queue.discard([index_job_id(item_id)])


def test_the_worker_reads_only_the_indexing_queue(settings: Settings) -> None:
    worker = build_worker(settings, pipeline_settings(indexing_concurrency=7))

    assert worker.queue_name == INDEXING_QUEUE
    assert set(worker.functions) == {INDEX_SAVED_ITEM_JOB, SWEEP_STUCK_ITEMS_JOB}
    assert worker.max_jobs == 7


def test_the_sweeper_runs_as_a_cron_at_startup_and_every_interval(settings: Settings) -> None:
    worker = build_worker(settings, pipeline_settings(indexing_sweep_interval_seconds=300))

    (sweep,) = worker.cron_jobs
    assert sweep.name == SWEEP_STUCK_ITEMS_JOB
    assert sweep.run_at_startup
    assert sweep.minute == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
    assert sweep.second == {0}


@pytest.mark.parametrize(
    ("interval", "minutes", "seconds"),
    [
        (15, None, {0, 15, 30, 45}),
        (60, set(range(60)), {0}),
        (900, {0, 15, 30, 45}, {0}),
        (3600, {0}, {0}),
    ],
)
def test_the_sweep_interval_becomes_cron_minutes_and_seconds(
    interval: int, minutes: set[int] | None, seconds: set[int]
) -> None:
    assert sweep_schedule(interval) == (minutes, seconds)
