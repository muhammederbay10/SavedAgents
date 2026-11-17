"""The background jobs ingestion queues, and the queue it puts them on.

Ingestion only queues jobs. The workers that run them belong to the indexing
pipeline and the response agent, and must register functions with these names
and keyword arguments:

| Job | Keyword arguments |
|---|---|
| `index_saved_item` | `saved_item_id` (text), `capture_event` (a `CaptureEvent` as JSON) |
| `answer_chat_message` | `chat_message` (a `ChatMessage` as JSON) |

Every job has an id made from what it works on. arq refuses a second job with
an id it still knows, which is how a webhook that Meta delivers twice ends up
as one job. arq knows an id while the job is queued or running, and afterwards
for as long as the worker keeps the job's result.
"""

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from arq.connections import ArqRedis
from redis.asyncio import ConnectionPool

from app.adapters.base import ChatMessage

# A job's keyword arguments are JSON-compatible values of any shape. arq takes
# them next to its own typed options, so they cannot be typed more narrowly than `Any`.
JobArguments = Mapping[str, Any]

INDEX_SAVED_ITEM_JOB = "index_saved_item"
ANSWER_CHAT_MESSAGE_JOB = "answer_chat_message"


def index_job_id(saved_item_id: UUID) -> str:
    """Return the id of the job that indexes one saved item.

    Args:
        saved_item_id: The saved item's id.

    Returns:
        The job id. It is the same for every share of the same item.
    """
    return f"index:{saved_item_id}"


def chat_job_id(message: ChatMessage) -> str:
    """Return the id of the job that answers one chat message.

    Args:
        message: The chat message.

    Returns:
        The job id. It is the same for every delivery of the same message.
    """
    return f"chat:{message.platform}:{message.message_id}"


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

    async def close(self) -> None:
        """Close the connections to Redis."""
        await self._redis.aclose(close_connection_pool=True)


def create_arq_job_queue(redis_url: str) -> ArqJobQueue:
    """Create the job queue for a Redis server.

    No connection is opened here: the first queued job opens one.

    Args:
        redis_url: The server's `redis://` URL.

    Returns:
        The job queue. Close it when the app shuts down.
    """
    return ArqJobQueue(ArqRedis(ConnectionPool.from_url(redis_url)))
