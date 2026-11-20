"""Pending "delete my data" requests, kept in Redis until the user confirms or they expire.

Deleting a user can't be undone, so it takes two steps (SAV-17, owner update of
20 Nov 2026): `request_data_deletion` stores a request here, and only the user's
**next** message can confirm it. The response agent takes the request out at
the start of every turn (`take`), so a "no" or any other message cancels it.
A request older than `DATA_DELETION_CONFIRM_SECONDS` counts as no request.

One key per user, `savely:agent:<user id>:deletion_request`, holds the time of
the request and expires on its own. It is erased with the user's data.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis

_KEY_PREFIX = "savely:agent"
_KEY_NAME = "deletion_request"
_MILLISECONDS_PER_SECOND = 1000


class DeletionRequests(Protocol):
    """Pending deletion requests, at most one per user; `RedisDeletionRequests` in production."""

    async def save(self, user_id: uuid.UUID, requested_at: datetime) -> None:
        """Store a request, replacing an earlier one of the same user."""
        ...

    async def take(self, user_id: uuid.UUID, now: datetime) -> bool:
        """Remove the user's request and tell whether it was still waiting."""
        ...


class RedisDeletionRequests(DeletionRequests):
    """Stores at most one pending deletion request per user in Redis."""

    def __init__(self, redis: Redis, lifetime: timedelta) -> None:
        """Create the store.

        Args:
            redis: The Redis client.
            lifetime: How long a request may wait for its confirmation
                (`DATA_DELETION_CONFIRM_SECONDS`).
        """
        self._redis = redis
        self._lifetime = lifetime

    async def save(self, user_id: uuid.UUID, requested_at: datetime) -> None:
        """Store a request, replacing an earlier one of the same user.

        Args:
            user_id: The DM's sender.
            requested_at: When they asked, with a time zone.

        Raises:
            ValueError: If `requested_at` has no time zone.
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        if requested_at.tzinfo is None:
            raise ValueError("requested_at must carry a time zone")
        lifetime_ms = max(1, int(self._lifetime.total_seconds() * _MILLISECONDS_PER_SECOND))
        await self._redis.set(_key(user_id), requested_at.isoformat(), px=lifetime_ms)

    async def take(self, user_id: uuid.UUID, now: datetime) -> bool:
        """Remove the user's request and tell whether it was still waiting.

        Args:
            user_id: The DM's sender.
            now: The current time, with a time zone.

        Returns:
            True when a request existed and is younger than the lifetime.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        stored = await self._redis.getdel(_key(user_id))
        if stored is None:
            return False
        requested_at = datetime.fromisoformat(stored.decode())
        return now - requested_at < self._lifetime

    async def erase_user(self, user_id: uuid.UUID, saved_item_ids: Sequence[uuid.UUID]) -> None:
        """Erase the user's pending request; part of "delete my data".

        Args:
            user_id: The user.
            saved_item_ids: Not needed; the argument matches `UserDataOutsidePostgres`.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        await self._redis.delete(_key(user_id))


def _key(user_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}:{user_id}:{_KEY_NAME}"
