"""The arq job: ingestion's contract, retries with a growing wait, and no content in its result."""

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from arq import Retry
from arq.connections import ArqRedis
from arq.jobs import Job

from app.config import Settings
from app.ingestion.jobs import (
    FINALIZE_SAVED_ITEM_JOB,
    INDEX_SAVED_ITEM_JOB,
    INDEXING_QUEUE,
    ArqJobQueue,
    index_job_id,
)
from app.pipeline.indexer import IndexOutcome, RetryLaterError
from app.pipeline.runs import Step
from app.pipeline.worker import WorkerResources, build_worker, index_saved_item
from tests.pipeline.support import pipeline_settings

MAX_TRIES = 3
RETRY_DELAY_SECONDS = 2.0


@dataclass
class StubIndexer:
    """Stands in for `ItemIndexer` and records how it was called."""

    fail_with_retry: bool = False
    calls: list[tuple[uuid.UUID, uuid.UUID, bool]] = field(default_factory=list)

    async def index(
        self, user_id: uuid.UUID, saved_item_id: uuid.UUID, *, last_try: bool
    ) -> IndexOutcome:
        self.calls.append((user_id, saved_item_id, last_try))
        if self.fail_with_retry and not last_try:
            raise RetryLaterError(Step.EXTRACT, TimeoutError())
        return IndexOutcome.FAILED if self.fail_with_retry else IndexOutcome.INDEXED


async def no_close() -> None:
    return None


def context(indexer: StubIndexer, job_try: int) -> dict[str, Any]:
    resources = WorkerResources(
        indexer=indexer,  # type: ignore[arg-type]
        max_tries=MAX_TRIES,
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
    assert indexer.calls == [(user_id, item_id, False)]


@pytest.mark.parametrize("job_try", [1, 2])
async def test_a_retryable_failure_asks_arq_to_try_again_later(job_try: int) -> None:
    indexer = StubIndexer(fail_with_retry=True)

    with pytest.raises(Retry) as raised:
        await index_saved_item(
            context(indexer, job_try), user_id=str(uuid.uuid4()), saved_item_id=str(uuid.uuid4())
        )

    assert raised.value.defer_score == RETRY_DELAY_SECONDS * job_try * 1000


async def test_the_last_try_marks_the_item_failed_instead_of_retrying() -> None:
    indexer = StubIndexer(fail_with_retry=True)

    result = await index_saved_item(
        context(indexer, MAX_TRIES), user_id=str(uuid.uuid4()), saved_item_id=str(uuid.uuid4())
    )

    assert result == "failed"
    assert indexer.calls[0][2] is True


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

        assert (user_id, item_id, False) in indexer.calls
        job = Job(index_job_id(item_id), ingestion_redis, _queue_name=INDEXING_QUEUE)
        assert await job.result(timeout=5) == "indexed"
    finally:
        await job_queue.discard([index_job_id(item_id)])


def test_the_worker_reads_only_the_indexing_queue(settings: Settings) -> None:
    worker = build_worker(settings, pipeline_settings(indexing_concurrency=7))

    assert worker.queue_name == INDEXING_QUEUE
    assert set(worker.functions) == {INDEX_SAVED_ITEM_JOB, FINALIZE_SAVED_ITEM_JOB}
    assert worker.max_jobs == 7
