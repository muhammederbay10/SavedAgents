"""The job queue: job ids, what is handed to arq, and arq's refusal of a known job id.

The last tests need a real Redis. They are skipped unless `TEST_REDIS_URL` is
set, and they remove the jobs they queue.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from arq.connections import ArqRedis
from arq.constants import default_queue_name, job_key_prefix
from redis.asyncio import ConnectionPool

from app.adapters.base import ChatMessage
from app.ingestion.jobs import (
    ANSWER_CHAT_MESSAGE_JOB,
    INDEX_SAVED_ITEM_JOB,
    ArqJobQueue,
    chat_job_id,
    create_arq_job_queue,
    index_job_id,
)

REDIS_URL_VARIABLE = "TEST_REDIS_URL"
SAVED_ITEM_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
MESSAGE_ID = "aWdfZAGFAKEMID0005"
ARGUMENTS = {"saved_item_id": str(SAVED_ITEM_ID), "capture_event": {"media_type": "reel"}}


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


def chat_message(platform: str = "instagram") -> ChatMessage:
    return ChatMessage(
        user_id=uuid.uuid4(),
        platform=platform,
        text="Restaurants in beşiktaş",
        message_id=MESSAGE_ID,
        received_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    )


def test_indexing_job_id_is_the_same_for_every_share_of_an_item() -> None:
    assert index_job_id(SAVED_ITEM_ID) == f"index:{SAVED_ITEM_ID}"


def test_chat_job_id_is_the_same_for_every_delivery_of_a_message() -> None:
    assert chat_job_id(chat_message()) == chat_job_id(chat_message())
    assert chat_job_id(chat_message()) == f"chat:instagram:{MESSAGE_ID}"


def test_chat_job_ids_of_two_platforms_never_collide() -> None:
    assert chat_job_id(chat_message("instagram")) != chat_job_id(chat_message("facebook"))


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
def redis_url() -> str:
    url = os.environ.get(REDIS_URL_VARIABLE)
    if not url:
        pytest.skip(f"{REDIS_URL_VARIABLE} is not set, so tests that need Redis are skipped")
    return url


@pytest.fixture
async def job_id(redis_url: str) -> AsyncIterator[str]:
    """A job id no other run uses. The job is removed from Redis afterwards."""
    job_id = f"test:{uuid.uuid4().hex}"
    yield job_id
    redis = ArqRedis(ConnectionPool.from_url(redis_url))
    try:
        await redis.delete(job_key_prefix + job_id)
        await redis.zrem(default_queue_name, job_id)
    finally:
        await redis.aclose(close_connection_pool=True)


async def test_redis_queue_holds_a_job_a_worker_can_read(redis_url: str, job_id: str) -> None:
    job_queue = create_arq_job_queue(redis_url)
    inspector = ArqRedis(ConnectionPool.from_url(redis_url))
    try:
        was_queued = await job_queue.enqueue(INDEX_SAVED_ITEM_JOB, job_id, ARGUMENTS)
        queued = [job for job in await inspector.queued_jobs() if job.job_id == job_id]
    finally:
        await job_queue.close()
        await inspector.aclose(close_connection_pool=True)

    (job,) = queued
    assert was_queued is True
    assert job.function == INDEX_SAVED_ITEM_JOB
    assert job.args == ()
    assert job.kwargs == ARGUMENTS


async def test_redis_queue_refuses_a_job_id_it_already_holds(redis_url: str, job_id: str) -> None:
    job_queue = create_arq_job_queue(redis_url)
    inspector = ArqRedis(ConnectionPool.from_url(redis_url))
    try:
        first = await job_queue.enqueue(ANSWER_CHAT_MESSAGE_JOB, job_id, {"chat_message": {}})
        second = await job_queue.enqueue(ANSWER_CHAT_MESSAGE_JOB, job_id, {"chat_message": {}})
        queued = [job for job in await inspector.queued_jobs() if job.job_id == job_id]
    finally:
        await job_queue.close()
        await inspector.aclose(close_connection_pool=True)

    assert (first, second) == (True, False)
    assert len(queued) == 1
