"""Ingestion service: stores captures, routes texts and queues the jobs that follow.

Each step commits to the database first and queues its job afterwards. If
queuing fails, the caller reports an error and the platform delivers the
message again. Every step can be repeated safely:

- a capture finds the item that is already stored, and is queued again only
  while the item is still waiting to be indexed;
- a job is queued under an id made from the item or the message, and the queue
  refuses an id it already knows;
- a collection hint that is already on the item is left as it is.
"""

import logging
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import CaptureEvent, ChatMessage, Platform
from app.db.models import ItemStatus
from app.ingestion.jobs import (
    ANSWER_CHAT_MESSAGE_JOB,
    INDEX_SAVED_ITEM_JOB,
    JobQueue,
    chat_job_id,
    index_job_id,
)
from app.ingestion.repository import attach_collection_hint, find_or_create_user, store_capture

logger = logging.getLogger(__name__)


class IngestionOutcome(StrEnum):
    """What ingestion did with one capture or one text."""

    ITEM_SAVED = "item_saved"
    ITEM_ALREADY_SAVED = "item_already_saved"
    COLLECTION_HINT = "collection_hint"
    CHAT_QUEUED = "chat_queued"
    CHAT_ALREADY_QUEUED = "chat_already_queued"


class IngestionService:
    """Turns capture events and chat messages into stored items and queued jobs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        job_queue: JobQueue,
        collection_hint_window: timedelta,
    ) -> None:
        """Create the service.

        Args:
            session_factory: Opens the database sessions.
            job_queue: Where indexing and chat jobs are queued.
            collection_hint_window: How long after a capture a text from the
                same user still counts as that item's collection hint.
        """
        self._session_factory = session_factory
        self._job_queue = job_queue
        self._collection_hint_window = collection_hint_window

    async def resolve_user(self, platform: Platform, platform_user_id: str) -> UUID:
        """Return our user for a platform account, creating both on first contact.

        Args:
            platform: The platform the account is on.
            platform_user_id: The platform's id of the account.

        Returns:
            The user's id.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database cannot be reached.
        """
        async with self._session_factory.begin() as session:
            return await find_or_create_user(session, platform, platform_user_id)

    async def ingest_capture(self, event: CaptureEvent) -> IngestionOutcome:
        """Store a capture as a saved item and queue its indexing.

        A post or a message that is already stored creates no second item. Its
        indexing is queued again only while the item is still pending, which
        covers a first delivery that stored the item but failed to queue it.

        Args:
            event: The capture event.

        Returns:
            Whether the capture created an item or found one.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database cannot be reached.
            redis.exceptions.RedisError: If the queue cannot be reached.
        """
        async with self._session_factory.begin() as session:
            stored = await store_capture(session, event)
        if stored.created or stored.status == ItemStatus.PENDING:
            await self._job_queue.enqueue(
                INDEX_SAVED_ITEM_JOB,
                index_job_id(stored.saved_item_id),
                {
                    "saved_item_id": str(stored.saved_item_id),
                    "capture_event": event.model_dump(mode="json"),
                },
            )
        outcome = (
            IngestionOutcome.ITEM_SAVED if stored.created else IngestionOutcome.ITEM_ALREADY_SAVED
        )
        logger.info(
            "capture_ingested",
            extra={
                "outcome": outcome,
                "saved_item_id": str(stored.saved_item_id),
                "user_id": str(event.user_id),
                "platform": event.platform,
                "media_type": event.media_type,
            },
        )
        return outcome

    async def ingest_chat_message(self, message: ChatMessage) -> IngestionOutcome:
        """Attach a text to the capture before it as a collection hint, or queue it as chat.

        Args:
            message: The chat message.

        Returns:
            Whether the text became a collection hint or a chat job, and
            whether that chat job was already queued.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database cannot be reached.
            redis.exceptions.RedisError: If the queue cannot be reached.
        """
        async with self._session_factory.begin() as session:
            is_collection_hint = await attach_collection_hint(
                session,
                user_id=message.user_id,
                platform=message.platform,
                hint=message.text,
                sent_at=message.received_at,
                window=self._collection_hint_window,
            )
        outcome = (
            IngestionOutcome.COLLECTION_HINT
            if is_collection_hint
            else await self._queue_chat_job(message)
        )
        logger.info(
            "chat_message_ingested",
            extra={
                "outcome": outcome,
                "user_id": str(message.user_id),
                "platform": message.platform,
            },
        )
        return outcome

    async def _queue_chat_job(self, message: ChatMessage) -> IngestionOutcome:
        was_queued = await self._job_queue.enqueue(
            ANSWER_CHAT_MESSAGE_JOB,
            chat_job_id(message),
            {"chat_message": message.model_dump(mode="json")},
        )
        return IngestionOutcome.CHAT_QUEUED if was_queued else IngestionOutcome.CHAT_ALREADY_QUEUED
