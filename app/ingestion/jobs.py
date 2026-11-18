"""The background jobs ingestion queues, and the queue it puts them on.

Ingestion only queues jobs. The workers that run them belong to the indexing
pipeline and the response agent, and must register functions with these names
and keyword arguments:

| Job | Keyword arguments | Field in the user's payloads hash |
|---|---|---|
| `index_saved_item` | `user_id`, `saved_item_id` | `capture:<saved_item_id>` |
| `answer_chat_message` | `user_id`, `message_receipt_id` | `chat:<message_receipt_id>` |

The arguments are ids only, as text. Captions, texts and media links stay in
the payloads hash (`app/ingestion/session_state.py`), which "delete my data"
erases. A worker that finds no payload stops: the user was deleted.

Every job has an id made from what it works on. arq refuses a second job with
an id it still knows, which is how a webhook that Meta delivers twice ends up
as one job. arq knows an id while the job is queued or running, and afterwards
for as long as the worker keeps the job's result.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from arq.connections import ArqRedis
from arq.constants import default_queue_name, job_key_prefix, result_key_prefix, retry_key_prefix
from redis.asyncio import ConnectionPool

# A job's keyword arguments are JSON-compatible values of any shape. arq takes
# them next to its own typed options, so they cannot be typed more narrowly than `Any`.
JobArguments = Mapping[str, Any]

INDEX_SAVED_ITEM_JOB = "index_saved_item"
ANSWER_CHAT_MESSAGE_JOB = "answer_chat_message"

# What arq keeps under a job id once the job is queued, retried or finished.
_JOB_KEY_PREFIXES = (job_key_prefix, retry_key_prefix, result_key_prefix)


def index_job_id(saved_item_id: UUID) -> str:
    """Return the id of the job that indexes one saved item.

    Args:
        saved_item_id: The saved item's id.

    Returns:
        The job id. It is the same for every share of the same item.
    """
    return f"index:{saved_item_id}"


def chat_job_id(message_receipt_id: UUID) -> str:
    """Return the id of the job that answers one chat message.

    Args:
        message_receipt_id: The id of the text's receipt.

    Returns:
        The job id. It is the same for every delivery of the same message.
    """
    return f"chat:{message_receipt_id}"


class JobQueue(Protocol):
    """Where ingestion puts background jobs."""

    async def enqueue(self, job_name: str, job_id: str, arguments: JobArguments) -> bool:
        """Queue a job, unless a job with the same id is already known.

        Args:
            job_name: The name of the worker function to run.
            job_id: The job's id. A second job with a known id is not queued.
            arguments: The function's keyword arguments, as JSON-compatible values.

        Returns:
            Whether the job was queued. `False` means the id was already known.
        """
        ...

    async def discard(self, job_ids: Sequence[str]) -> None:
        """Remove jobs from the queue, with their retry state and kept results.

        Args:
            job_ids: The ids of the jobs. Unknown ids are skipped.
        """
        ...


class ArqJobQueue:
    """A `JobQueue` on Redis, read by arq workers."""

    def __init__(self, redis: ArqRedis) -> None:
        """Wrap an arq Redis client.

        Args:
            redis: The client to queue jobs through.
        """
        self._redis = redis

    async def enqueue(self, job_name: str, job_id: str, arguments: JobArguments) -> bool:
        """Queue a job on Redis, unless arq already knows the job id.

        Args:
            job_name: The name of the worker function to run.
            job_id: The job's id.
            arguments: The function's keyword arguments.

        Returns:
            Whether the job was queued.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        job = await self._redis.enqueue_job(job_name, _job_id=job_id, **arguments)
        return job is not None

    async def discard(self, job_ids: Sequence[str]) -> None:
        """Remove jobs from arq's queue, with their retry state and kept results.

        A job a worker is running at this moment finishes. arq then keeps its
        result, which holds only the job's ids.

        Args:
            job_ids: The ids of the jobs.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        if not job_ids:
            return
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.zrem(default_queue_name, *job_ids)
            pipeline.delete(
                *(prefix + job_id for job_id in job_ids for prefix in _JOB_KEY_PREFIXES)
            )
            await pipeline.execute()


def create_arq_redis(redis_url: str) -> ArqRedis:
    """Create the Redis client for the job queue and ingestion's state.

    No connection is opened here: the first command opens one.

    Args:
        redis_url: The server's `redis://` URL.

    Returns:
        The client. Close it with `aclose(close_connection_pool=True)`.
    """
    return ArqRedis(ConnectionPool.from_url(redis_url))
