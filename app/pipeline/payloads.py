"""Reads the capture event an indexing job works on, from ingestion's state in Redis.

The job carries only ids (decision record 0009). The event, with the media link
the pipeline downloads, is in the user's payloads hash, which "delete my data"
erases. No payload means the user was deleted, and the job stops.
"""

import uuid
from collections.abc import Awaitable
from typing import Protocol, cast

from redis.asyncio import Redis

from app.adapters.base import CaptureEvent
from app.ingestion.session_state import capture_payload_field, payloads_key


class CaptureSource(Protocol):
    """Where an indexing job finds the capture event of its item."""

    async def capture(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> CaptureEvent | None:
        """Return the capture event of an item, if it is still kept.

        Args:
            user_id: The item's owner.
            saved_item_id: The item.

        Returns:
            The event, or `None` when the user was deleted.
        """
        ...


class CapturePayloads(CaptureSource):
    """Reads indexing payloads from ingestion's per-user hash."""

    def __init__(self, redis: Redis) -> None:
        """Create the reader.

        Args:
            redis: The Redis client ingestion writes to.
        """
        self._redis = redis

    async def capture(self, user_id: uuid.UUID, saved_item_id: uuid.UUID) -> CaptureEvent | None:
        """Return the capture event of an item, if it is still kept.

        Args:
            user_id: The item's owner.
            saved_item_id: The item.

        Returns:
            The event, or `None` when there is none: the user was deleted, or
            the payload outlived its lifetime.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        # redis-py declares its commands for its blocking and async clients at
        # once; the async client always returns an awaitable.
        value = await cast(
            Awaitable[bytes | None],
            self._redis.hget(payloads_key(user_id), capture_payload_field(saved_item_id)),
        )
        return None if value is None else CaptureEvent.model_validate_json(value)
