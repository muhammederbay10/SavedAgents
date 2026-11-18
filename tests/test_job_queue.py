"""The job queue: job ids, what is handed to arq, and how jobs are queued and discarded.

The last tests need a real Redis. They are skipped unless `TEST_REDIS_URL` is
set, and they remove the jobs they queue.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from arq.connections import ArqRedis
from arq.constants import default_queue_name, job_key_prefix, result_key_prefix, retry_key_prefix

from app.ingestion.jobs import (
    ANSWER_CHAT_MESSAGE_JOB,
    INDEX_SAVED_ITEM_JOB,
    ArqJobQueue,
    chat_job_id,
    index_job_id,
)

SAVED_ITEM_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
RECEIPT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
ARGUMENTS = {"user_id": str(USER_ID), "saved_item_id": str(SAVED_ITEM_ID)}


@dataclass
class ArqRedisStub:
    """Answers `enqueue_job` like arq: a job object, or `None` for a known job id."""

    known_job_ids: set[str] = field(default_factory=set)
    calls: list[tuple[str, str | None, dict[str, object]]] = field(default_factory=list)

    async def enqueue_job(
        self, function: str, *, _job_id: str | None = None, **kwargs: object
    ) -> object | None:
        self.calls.append((function, _job_id, kwargs))
        if _job_id in self.known_job_ids:
            return None
        return object()


def test_indexing_job_id_is_the_same_for_every_share_of_an_item() -> None:
    assert index_job_id(SAVED_ITEM_ID) == f"index:{SAVED_ITEM_ID}"


def test_chat_job_id_is_named_after_the_texts_receipt() -> None:
    assert chat_job_id(RECEIPT_ID) == f"chat:{RECEIPT_ID}"


async def test_job_is_handed_to_arq_with_its_name_id_and_keyword_arguments() -> None:
    redis = ArqRedisStub()

    was_queued = await ArqJobQueue(redis).enqueue(INDEX_SAVED_ITEM_JOB, "index:1", ARGUMENTS)

    assert was_queued is True
    assert redis.calls == [(INDEX_SAVED_ITEM_JOB, "index:1", ARGUMENTS)]


async def test_job_id_arq_already_knows_is_reported_as_not_queued() -> None:
    redis = ArqRedisStub(known_job_ids={"index:1"})

    was_queued = await ArqJobQueue(redis).enqueue(INDEX_SAVED_ITEM_JOB, "index:1", ARGUMENTS)

    assert was_queued is False


# --- With a real Redis ---------------------------------------------------------


@pytest.fixture
async def job_id(ingestion_redis: ArqRedis) -> AsyncIterator[str]:
    """A job id no other run uses. Its keys are removed from Redis afterwards."""
    job_id = f"test:{uuid.uuid4().hex}"
    yield job_id
    await ingestion_redis.delete(*(prefix + job_id for prefix in KEY_PREFIXES))
    await ingestion_redis.zrem(default_queue_name, job_id)


KEY_PREFIXES = (job_key_prefix, retry_key_prefix, result_key_prefix)


async def queued_job_ids(redis: ArqRedis) -> set[str]:
    return {job.job_id for job in await redis.queued_jobs()}


async def test_redis_queue_holds_a_job_a_worker_can_read(
    ingestion_redis: ArqRedis, job_id: str
) -> None:
    was_queued = await ArqJobQueue(ingestion_redis).enqueue(INDEX_SAVED_ITEM_JOB, job_id, ARGUMENTS)

    queued = [job for job in await ingestion_redis.queued_jobs() if job.job_id == job_id]
    (job,) = queued
    assert was_queued is True
    assert job.function == INDEX_SAVED_ITEM_JOB
    assert job.args == ()
    assert job.kwargs == ARGUMENTS


async def test_redis_queue_refuses_a_job_id_it_already_holds(
    ingestion_redis: ArqRedis, job_id: str
) -> None:
    job_queue = ArqJobQueue(ingestion_redis)
    arguments = {"user_id": str(USER_ID), "message_receipt_id": str(RECEIPT_ID)}

    first = await job_queue.enqueue(ANSWER_CHAT_MESSAGE_JOB, job_id, arguments)
    second = await job_queue.enqueue(ANSWER_CHAT_MESSAGE_JOB, job_id, arguments)

    queued = [job for job in await ingestion_redis.queued_jobs() if job.job_id == job_id]
    assert (first, second) == (True, False)
    assert len(queued) == 1


async def test_discarded_job_leaves_no_job_retry_state_or_result_and_can_be_queued_again(
    ingestion_redis: ArqRedis, job_id: str
) -> None:
    job_queue = ArqJobQueue(ingestion_redis)
    await job_queue.enqueue(INDEX_SAVED_ITEM_JOB, job_id, ARGUMENTS)
    # What a worker leaves behind after a retry and after finishing the job.
    await ingestion_redis.set(retry_key_prefix + job_id, b"1")
    await ingestion_redis.set(result_key_prefix + job_id, b"synthetic result")

    await job_queue.discard([job_id])

    assert job_id not in await queued_job_ids(ingestion_redis)
    assert await ingestion_redis.exists(*(prefix + job_id for prefix in KEY_PREFIXES)) == 0
    assert await job_queue.enqueue(INDEX_SAVED_ITEM_JOB, job_id, ARGUMENTS) is True


async def test_discarding_jobs_keeps_the_others(ingestion_redis: ArqRedis, job_id: str) -> None:
    job_queue = ArqJobQueue(ingestion_redis)
    other_job_id = f"{job_id}:other"
    await job_queue.enqueue(INDEX_SAVED_ITEM_JOB, job_id, ARGUMENTS)
    await job_queue.enqueue(INDEX_SAVED_ITEM_JOB, other_job_id, ARGUMENTS)

    try:
        await job_queue.discard([job_id, f"{job_id}:never-queued"])
        await job_queue.discard([])

        assert other_job_id in await queued_job_ids(ingestion_redis)
        assert job_id not in await queued_job_ids(ingestion_redis)
    finally:
        await job_queue.discard([other_job_id])
