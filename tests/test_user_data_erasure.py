"""Delete my data also erases what ingestion left in Redis for the user.

These tests need Postgres with PostGIS and pgvector (see `tests/database.py`)
and a Redis server (`TEST_REDIS_URL`). All ids, links and texts are synthetic.
"""

import uuid
from datetime import UTC, datetime, timedelta

from arq.connections import ArqRedis
from arq.constants import result_key_prefix
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import CaptureEvent, ChatMessage
from app.db.deletion import delete_user_data
from app.ingestion.erasure import IngestionDataEraser
from app.ingestion.jobs import ArqJobQueue
from app.ingestion.service import IngestionService
from app.ingestion.session_state import RedisIngestionState
from tests.db_factories import INSTAGRAM
from tests.redis_inspection import all_stored_bytes

HINT_WINDOW = timedelta(seconds=30)
SHARED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
LEAVING_SENDER_ID = "1000000000000031"
STAYING_SENDER_ID = "1000000000000032"
# What arq keeps once a worker has run a job. The real result holds the job's arguments.
SYNTHETIC_RESULT = b"synthetic job result"


def private_values(label: str) -> dict[str, str]:
    """Texts that belong to one user. Each is unique to the test run."""
    marker = f"{label}-{uuid.uuid4().hex}"
    return {
        "caption": f"synthetic caption {marker}",
        "media_url": (
            "https://lookaside.fbsbx.com/ig_messaging_cdn/"
            f"?asset_id=18000000000000901&signature=FAKE_SIGNATURE_{marker}"
        ),
        "chat_text": f"synthetic question {marker}",
        "message_id": f"aWdfZAGFAKEMID{marker}",
    }


async def ingest_share_and_question(
    ingestion: IngestionService, sender_id: str, values: dict[str, str]
) -> uuid.UUID:
    user_id = await ingestion.resolve_user(INSTAGRAM, sender_id)
    await ingestion.ingest_capture(
        CaptureEvent(
            user_id=user_id,
            platform=INSTAGRAM,
            source="dm",
            post_url=None,
            platform_post_id=None,
            caption=values["caption"],
            media_url=values["media_url"],
            media_type="post",
            collection_hint=None,
            source_message_id=f"{values['message_id']}-share",
            received_at=SHARED_AT,
        )
    )
    await ingestion.ingest_chat_message(
        ChatMessage(
            user_id=user_id,
            platform=INSTAGRAM,
            text=values["chat_text"],
            message_id=f"{values['message_id']}-text",
            # Outside the hint window, so the text is queued as chat.
            received_at=SHARED_AT + HINT_WINDOW * 2,
        )
    )
    return user_id


async def job_ids_by_user(redis: ArqRedis) -> dict[str, set[str]]:
    jobs: dict[str, set[str]] = {}
    for job in await redis.queued_jobs():
        jobs.setdefault(str(job.kwargs["user_id"]), set()).add(job.job_id)
    return jobs


async def test_deleting_a_user_erases_their_queued_captions_texts_and_media_links(
    session_factory: async_sessionmaker[AsyncSession],
    ingestion_redis: ArqRedis,
    ingestion_state: RedisIngestionState,
    redis_url: str,
) -> None:
    job_queue = ArqJobQueue(ingestion_redis)
    ingestion = IngestionService(session_factory, job_queue, ingestion_state, HINT_WINDOW)
    leaving = private_values("leaving")
    staying = private_values("staying")
    leaving_user_id = await ingest_share_and_question(ingestion, LEAVING_SENDER_ID, leaving)
    staying_user_id = await ingest_share_and_question(ingestion, STAYING_SENDER_ID, staying)
    jobs_before = await job_ids_by_user(ingestion_redis)
    leaving_jobs = jobs_before[str(leaving_user_id)]
    for job_id in leaving_jobs:
        await ingestion_redis.set(result_key_prefix + job_id, SYNTHETIC_RESULT)
    stored_before = await all_stored_bytes(redis_url)

    try:
        async with session_factory.begin() as session:
            await delete_user_data(
                session, leaving_user_id, IngestionDataEraser(ingestion_state, job_queue)
            )

        stored_after = await all_stored_bytes(redis_url)
        jobs_after = await job_ids_by_user(ingestion_redis)
        assert all(value.encode() in stored_before for value in leaving.values())
        assert [name for name, value in leaving.items() if value.encode() in stored_after] == []
        assert str(leaving_user_id).encode() not in stored_after
        assert len(leaving_jobs) == 2
        assert str(leaving_user_id) not in jobs_after
        kept_results = [
            job_id
            for job_id in leaving_jobs
            if await ingestion_redis.exists(result_key_prefix + job_id)
        ]
        assert kept_results == []
        assert all(value.encode() in stored_after for value in staying.values())
        assert jobs_after[str(staying_user_id)] == jobs_before[str(staying_user_id)]
    finally:
        await job_queue.discard(sorted(jobs_before.get(str(staying_user_id), set())))
        await ingestion_state.erase_user(staying_user_id)
