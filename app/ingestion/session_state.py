"""What ingestion keeps in Redis about a user's recent messages.

Three hashes per user, named `savedagent:ingestion:<user id>:<name>`, hold
everything ingestion writes to Redis about a user, so "delete my data" can
erase it by deleting three keys:

| Name | Field | Value, as JSON |
|---|---|---|
| `payloads` | `capture:<saved item id>` | The `CaptureEvent` an indexing job works on |
| | `chat:<receipt id>` | The `ChatMessage` a chat job answers |
| `receipts` | `<platform>:<message id>` | How a text was routed (`TextReceipt`) |
| `latest_shares` | `<platform>` | The user's latest share (`LatestShare`) |

The jobs on the queue carry only ids. A worker reads the payload from here, and
finds nothing once the user is deleted.

Every write sets the key to expire after the configured lifetime. It must be
longer than the time Meta keeps delivering a failed webhook again, so that a
late delivery still finds its receipt.

Callers hold the user's row lock (`app.ingestion.repository.lock_user`) while
they read and write a user's keys. That makes each read-then-write safe and
keeps writes from racing "delete my data".
"""

import uuid
from collections.abc import Awaitable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

from app.adapters.base import CaptureEvent, ChatMessage

_KEY_PREFIX = "savedagent:ingestion"
_PAYLOADS = "payloads"
_RECEIPTS = "receipts"
_LATEST_SHARES = "latest_shares"
_CAPTURE_PAYLOAD_PREFIX = "capture"
_CHAT_PAYLOAD_PREFIX = "chat"


class TextRoute(StrEnum):
    """Where a text went: onto a just-shared item as its collection, or to the response agent."""

    COLLECTION_HINT = "collection_hint"
    CHAT = "chat"


class TextReceipt(BaseModel):
    """How one text was routed, kept so a repeated delivery is routed the same way.

    Attributes:
        id: The receipt's own id. The chat job and its payload are named after it.
        route: Where the text went.
        saved_item_id: The item the text labels, for a collection hint.
        chat_queued: Whether the chat job was queued, for a chat message.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    route: TextRoute
    saved_item_id: uuid.UUID | None
    chat_queued: bool


class LatestShare(BaseModel):
    """The latest share from a user: when it was sent, and the item it is stored as.

    A share of a post that is already saved points to the existing item, with
    the time of the new share.

    Attributes:
        saved_item_id: The item the share is stored as.
        sent_at: When the user sent the share.
    """

    model_config = ConfigDict(frozen=True)

    saved_item_id: uuid.UUID
    sent_at: datetime


class RedisIngestionState:
    """Ingestion's per-user state in Redis: payloads, text receipts and latest shares."""

    def __init__(self, redis: Redis, lifetime: timedelta) -> None:
        """Create the state store.

        Args:
            redis: The Redis client to use.
            lifetime: How long a user's keys live after their last write.
        """
        self._redis = redis
        self._lifetime = lifetime

    async def save_capture_payload(
        self, user_id: uuid.UUID, saved_item_id: uuid.UUID, event: CaptureEvent
    ) -> None:
        """Keep the capture event an indexing job works on.

        A later share of the same item replaces it, so the job gets the newest
        media link.

        Args:
            user_id: The user who shared the post.
            saved_item_id: The item the job indexes.
            event: The capture event.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        field = capture_payload_field(saved_item_id)
        await self._write(user_id, _PAYLOADS, field, event.model_dump_json())

    async def save_chat_payload(
        self, user_id: uuid.UUID, receipt_id: uuid.UUID, message: ChatMessage
    ) -> None:
        """Keep the chat message a chat job answers.

        Args:
            user_id: The user who wrote the text.
            receipt_id: The id of the text's receipt.
            message: The chat message.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        field = chat_payload_field(receipt_id)
        await self._write(user_id, _PAYLOADS, field, message.model_dump_json())

    async def record_share(
        self, user_id: uuid.UUID, platform: str, saved_item_id: uuid.UUID, sent_at: datetime
    ) -> None:
        """Remember a share as the user's latest, unless a later one is already known.

        Webhooks can arrive out of order, so the times decide, not the order of arrival.

        Args:
            user_id: The user who shared the post.
            platform: The platform it was shared on.
            saved_item_id: The item the share is stored as.
            sent_at: When the user sent the share.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        known = await self.latest_share(user_id, platform)
        if known is not None and known.sent_at > sent_at:
            return
        share = LatestShare(saved_item_id=saved_item_id, sent_at=sent_at)
        await self._write(user_id, _LATEST_SHARES, platform, share.model_dump_json())

    async def latest_share(self, user_id: uuid.UUID, platform: str) -> LatestShare | None:
        """Return the user's latest share on a platform, if one is known.

        Args:
            user_id: The user.
            platform: The platform.

        Returns:
            The latest share, or `None`.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        value = await self._hget(_key(user_id, _LATEST_SHARES), platform)
        return None if value is None else LatestShare.model_validate_json(value)

    async def text_receipt(
        self, user_id: uuid.UUID, platform: str, message_id: str
    ) -> TextReceipt | None:
        """Return how a text was routed, if it was seen before.

        Args:
            user_id: The user who wrote the text.
            platform: The platform it was sent on.
            message_id: The platform's id of the message.

        Returns:
            The text's receipt, or `None` for a text seen for the first time.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        field = _receipt_field(platform, message_id)
        value = await self._hget(_key(user_id, _RECEIPTS), field)
        return None if value is None else TextReceipt.model_validate_json(value)

    async def save_text_receipt(
        self, user_id: uuid.UUID, platform: str, message_id: str, receipt: TextReceipt
    ) -> None:
        """Keep how a text was routed.

        Args:
            user_id: The user who wrote the text.
            platform: The platform it was sent on.
            message_id: The platform's id of the message.
            receipt: The receipt to keep.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        field = _receipt_field(platform, message_id)
        await self._write(user_id, _RECEIPTS, field, receipt.model_dump_json())

    async def chat_receipt_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        """Return the receipt ids of the user's texts that were routed to chat.

        Args:
            user_id: The user.

        Returns:
            The ids, which name the user's chat jobs.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        values = await self._hvals(_key(user_id, _RECEIPTS))
        receipts = [TextReceipt.model_validate_json(value) for value in values]
        return [receipt.id for receipt in receipts if receipt.route is TextRoute.CHAT]

    async def erase_user(self, user_id: uuid.UUID) -> None:
        """Delete everything ingestion keeps in Redis for a user.

        Args:
            user_id: The user.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        await self._redis.delete(*(_key(user_id, name) for name in _ALL_KEY_NAMES))

    # redis-py declares its commands once for its blocking and its async client,
    # so their return types are unions. The async client always returns an awaitable.
    async def _hget(self, key: str, field: str) -> bytes | None:
        return await cast(Awaitable[bytes | None], self._redis.hget(key, field))

    async def _hvals(self, key: str) -> list[bytes]:
        return await cast(Awaitable[list[bytes]], self._redis.hvals(key))

    async def _write(self, user_id: uuid.UUID, key_name: str, field: str, value: str) -> None:
        key = _key(user_id, key_name)
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.hset(key, field, value)
            pipeline.expire(key, self._lifetime)
            await pipeline.execute()


_ALL_KEY_NAMES = (_PAYLOADS, _RECEIPTS, _LATEST_SHARES)


def payloads_key(user_id: uuid.UUID) -> str:
    """Return the name of the hash that holds a user's job payloads.

    Args:
        user_id: The user.

    Returns:
        The Redis key.
    """
    return _key(user_id, _PAYLOADS)


def capture_payload_field(saved_item_id: uuid.UUID) -> str:
    """Return the field that holds the capture event of an indexing job.

    Args:
        saved_item_id: The item the job indexes.

    Returns:
        The field in the user's payloads hash.
    """
    return f"{_CAPTURE_PAYLOAD_PREFIX}:{saved_item_id}"


def chat_payload_field(receipt_id: uuid.UUID) -> str:
    """Return the field that holds the chat message of a chat job.

    Args:
        receipt_id: The id of the text's receipt.

    Returns:
        The field in the user's payloads hash.
    """
    return f"{_CHAT_PAYLOAD_PREFIX}:{receipt_id}"


def _key(user_id: uuid.UUID, name: str) -> str:
    return f"{_KEY_PREFIX}:{user_id}:{name}"


def _receipt_field(platform: str, message_id: str) -> str:
    return f"{platform}:{message_id}"
