"""Erases what ingestion keeps about a user outside Postgres: job payloads, receipts, jobs.

`app.db.deletion.delete_user_data` calls it while it holds the user's row lock,
before it deletes the user's rows.
"""

import uuid
from collections.abc import Sequence

from app.ingestion.jobs import JobQueue, chat_job_id, index_job_id
from app.ingestion.session_state import RedisIngestionState


class IngestionDataEraser:
    """Removes one user's ingestion state and queued jobs from Redis."""

    def __init__(self, state: RedisIngestionState, job_queue: JobQueue) -> None:
        """Create the eraser.

        Args:
            state: Ingestion's per-user state.
            job_queue: The queue the user's jobs are on.
        """
        self._state = state
        self._job_queue = job_queue

    async def erase_user(self, user_id: uuid.UUID, saved_item_ids: Sequence[uuid.UUID]) -> None:
        """Remove a user's indexing and chat jobs, and everything ingestion keeps for them.

        Args:
            user_id: The user.
            saved_item_ids: The user's saved items, which name their indexing jobs.

        Raises:
            redis.exceptions.RedisError: If Redis cannot be reached.
        """
        chat_receipt_ids = await self._state.chat_receipt_ids(user_id)
        job_ids = [
            *(index_job_id(item_id) for item_id in saved_item_ids),
            *(chat_job_id(receipt_id) for receipt_id in chat_receipt_ids),
        ]
        await self._job_queue.discard(job_ids)
        await self._state.erase_user(user_id)
