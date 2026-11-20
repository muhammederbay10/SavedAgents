""" "Delete my data" from the DM: two steps enforced in code, and one eraser for every store.

The text model is scripted (fake). These tests need Postgres with PostGIS and
pgvector (`TEST_DATABASE_ADMIN_URL`) and Redis (`TEST_REDIS_URL`). Every id,
text and file is synthetic.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from arq.connections import ArqRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import CaptureEvent, ChatMessage
from app.agent.agent import AgentAnswer, AgentTurn
from app.agent.deletion_requests import RedisDeletionRequests
from app.agent.replies.templates import deletion_confirmation, deletion_done, reply_wording
from app.agent.tools import (
    CONFIRM_DATA_DELETION_TOOL,
    REQUEST_DATA_DELETION_TOOL,
    SEARCH_ITEMS_TOOL,
    PlatformRecipient,
)
from app.db.deletion import UserDataEraser, UserDataOutsidePostgres
from app.db.models import PlatformAccount, SavedItem, User
from app.ingestion.erasure import IngestionDataEraser
from app.ingestion.jobs import CHAT_QUEUE, INDEXING_QUEUE, ArqJobQueue
from app.ingestion.service import IngestionService
from app.ingestion.session_state import RedisIngestionState
from app.models.base import GenerationResult
from app.pipeline.media.storage import LocalMediaStore
from tests.agent.agent_support import (
    CONFIRM_SECONDS,
    AgentUnderTest,
    FakeClock,
    build_agent,
    final_answer,
    structured_reply,
    tool_calls,
    tool_results,
    translated_wording,
)
from tests.db_factories import INSTAGRAM
from tests.redis_inspection import all_stored_bytes

HINT_WINDOW = timedelta(seconds=30)
LEAVING_SENDER_ID = "1000000000000171"
STAYING_SENDER_ID = "1000000000000172"
CONFIRM_MINUTES = CONFIRM_SECONDS // 60
VIDEO = b"synthetic video bytes"

REQUEST = tool_calls((REQUEST_DATA_DELETION_TOOL, {}))
CONFIRM = tool_calls((CONFIRM_DATA_DELETION_TOOL, {}))


class FailingMediaStore:
    """A media store whose erase fails, as a full disk or a lost mount would."""

    async def erase_user(self, user_id: uuid.UUID, saved_item_ids: Sequence[uuid.UUID]) -> None:
        raise OSError("synthetic media store failure")


@dataclass
class Stores:
    sessions: async_sessionmaker[AsyncSession]
    redis: ArqRedis
    state: RedisIngestionState
    jobs: ArqJobQueue
    media: LocalMediaStore
    media_root: Path
    requests: RedisDeletionRequests
    clock: FakeClock

    def outside_postgres(
        self, media: UserDataOutsidePostgres | None = None
    ) -> list[UserDataOutsidePostgres]:
        return [
            IngestionDataEraser(self.state, self.jobs),
            self.requests,
            media or self.media,
        ]

    def agent(self, script: Sequence[GenerationResult]) -> AgentUnderTest:
        return build_agent(
            self.sessions,
            script,
            deletion_requests=self.requests,
            outside_postgres=self.outside_postgres(),
            clock=self.clock,
        )


@dataclass
class SavedUser:
    user_id: uuid.UUID
    sender_id: str
    item_id: uuid.UUID
    marker: str


@pytest.fixture
async def stores(
    session_factory: async_sessionmaker[AsyncSession],
    ingestion_redis: ArqRedis,
    ingestion_state: RedisIngestionState,
    tmp_path: Path,
) -> AsyncIterator[Stores]:
    stores = Stores(
        sessions=session_factory,
        redis=ingestion_redis,
        state=ingestion_state,
        jobs=ArqJobQueue(ingestion_redis),
        media=LocalMediaStore(tmp_path / "media"),
        media_root=tmp_path / "media",
        requests=RedisDeletionRequests(ingestion_redis, timedelta(seconds=CONFIRM_SECONDS)),
        clock=FakeClock(),
    )
    yield stores


async def save_something(stores: Stores, sender_id: str) -> SavedUser:
    """A user with a shared reel, a queued question and a stored media file."""
    marker = f"synthetic-{uuid.uuid4().hex}"
    ingestion = IngestionService(stores.sessions, stores.jobs, stores.state, HINT_WINDOW)
    user_id = await ingestion.resolve_user(INSTAGRAM, sender_id)
    await ingestion.ingest_capture(
        CaptureEvent(
            user_id=user_id,
            platform=INSTAGRAM,
            source="dm",
            post_url=None,
            platform_post_id=None,
            caption=f"caption {marker}",
            media_url=f"https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1&sig={marker}",
            media_type="post",
            collection_hint=None,
            source_message_id=f"mid-{marker}-share",
            received_at=stores.clock.now,
        )
    )
    await ingestion.ingest_chat_message(
        ChatMessage(
            user_id=user_id,
            platform=INSTAGRAM,
            text=f"question {marker}",
            message_id=f"mid-{marker}-text",
            received_at=stores.clock.now + HINT_WINDOW * 2,
        )
    )
    async with stores.sessions() as session:
        item_id = await session.scalar(select(SavedItem.id).where(SavedItem.user_id == user_id))
    assert item_id is not None
    await stores.media.put(user_id, item_id, "video.mp4", VIDEO)
    return SavedUser(user_id=user_id, sender_id=sender_id, item_id=item_id, marker=marker)


async def answer(stores: Stores, agent: AgentUnderTest, user: SavedUser, text: str) -> AgentAnswer:
    return await agent.agent.answer(AgentTurn(user_id=user.user_id, country=None, text=text))


async def user_exists(stores: Stores, user_id: uuid.UUID) -> bool:
    async with stores.sessions() as session:
        return await session.get(User, user_id) is not None


async def queued_job_users(redis: ArqRedis) -> set[str]:
    jobs = [
        *await redis.queued_jobs(queue_name=INDEXING_QUEUE),
        *await redis.queued_jobs(queue_name=CHAT_QUEUE),
    ]
    return {str(job.kwargs["user_id"]) for job in jobs}


async def clean_up(stores: Stores, *users: SavedUser) -> None:
    for user in users:
        await stores.state.erase_user(user.user_id)
        await stores.requests.erase_user(user.user_id, ())
    await stores.jobs.discard(
        [
            job.job_id
            for queue in (INDEXING_QUEUE, CHAT_QUEUE)
            for job in await stores.redis.queued_jobs(queue_name=queue)
            if str(job.kwargs["user_id"]) in {str(user.user_id) for user in users}
        ]
    )


@pytest.mark.parametrize(
    ("language", "text"),
    [("en", "delete my data"), ("tr", "verilerimi sil"), ("es", "borra mis datos")],
)
async def test_a_request_in_any_language_gets_the_confirmation_question(
    stores: Stores, language: str, text: str
) -> None:
    user = await save_something(stores, LEAVING_SENDER_ID)
    script = [REQUEST, final_answer(language, "…")]
    if language == "es":
        script.append(structured_reply(translated_wording("es")))
    agent = stores.agent(script)

    try:
        result = await answer(stores, agent, user, text)

        if language == "es":
            expected = translated_wording("es")["deletion_confirmation"].format(
                minutes=CONFIRM_MINUTES
            )
        else:
            expected = deletion_confirmation(CONFIRM_MINUTES, reply_wording(language)).text
        assert result.messages.intro == expected
        assert result.messages.results == ()
        assert result.deleted_recipients == ()
        assert await user_exists(stores, user.user_id)
    finally:
        await clean_up(stores, user)


async def test_yes_on_the_next_message_erases_every_store_and_leaves_others_alone(
    stores: Stores, redis_url: str
) -> None:
    leaving = await save_something(stores, LEAVING_SENDER_ID)
    staying = await save_something(stores, STAYING_SENDER_ID)
    agent = stores.agent(
        [REQUEST, final_answer("en", "…"), CONFIRM, final_answer("en", "Deleted.")]
    )

    try:
        await answer(stores, agent, leaving, "delete my data")
        stores.clock.advance(CONFIRM_SECONDS - 1)
        result = await answer(stores, agent, leaving, "yes")

        (confirmed,) = tool_results(agent.generator.requests[3])
        assert confirmed == {"deleted": True, "saved_items": 1}
        assert result.messages.intro == deletion_done(reply_wording("en")).text
        assert result.deleted_recipients == (PlatformRecipient(INSTAGRAM, LEAVING_SENDER_ID),)
        assert not await user_exists(stores, leaving.user_id)
        async with stores.sessions() as session:
            leftover_accounts = await session.scalar(
                select(func.count())
                .select_from(PlatformAccount)
                .where(PlatformAccount.platform_user_id == LEAVING_SENDER_ID)
            )
        assert leftover_accounts == 0
        stored = await all_stored_bytes(redis_url)
        assert leaving.marker.encode() not in stored
        assert str(leaving.user_id).encode() not in stored
        assert str(leaving.user_id) not in await queued_job_users(stores.redis)
        assert not (stores.media_root / str(leaving.user_id)).exists()

        assert await user_exists(stores, staying.user_id)
        assert staying.marker.encode() in stored
        assert str(staying.user_id) in await queued_job_users(stores.redis)
        assert (stores.media_root / str(staying.user_id) / str(staying.item_id)).exists()
    finally:
        await clean_up(stores, leaving, staying)


@pytest.mark.parametrize(
    ("reply", "next_turn"),
    [
        ("no", final_answer("en", "Okay, nothing was deleted.")),
        ("cafés in Brooklyn?", tool_calls((SEARCH_ITEMS_TOOL, {"category": "café"}))),
    ],
)
async def test_no_or_any_other_message_cancels_the_request(
    stores: Stores, reply: str, next_turn: GenerationResult
) -> None:
    user = await save_something(stores, LEAVING_SENDER_ID)
    script = [REQUEST, final_answer("en", "…"), next_turn]
    if next_turn.tool_calls:
        script.append(final_answer("en", "No cafés saved there."))
    script.extend([CONFIRM, final_answer("en", "Nothing to confirm.")])
    agent = stores.agent(script)

    try:
        await answer(stores, agent, user, "delete my data")
        await answer(stores, agent, user, reply)
        await answer(stores, agent, user, "yes")

        assert "Nothing was deleted" in str(tool_results(agent.generator.requests[-1])[-1])
        assert await user_exists(stores, user.user_id)
    finally:
        await clean_up(stores, user)


async def test_an_expired_request_deletes_nothing(stores: Stores) -> None:
    user = await save_something(stores, LEAVING_SENDER_ID)
    agent = stores.agent(
        [REQUEST, final_answer("en", "…"), CONFIRM, final_answer("en", "It expired.")]
    )

    try:
        await answer(stores, agent, user, "delete my data")
        stores.clock.advance(CONFIRM_SECONDS)
        await answer(stores, agent, user, "yes")

        (refused,) = tool_results(agent.generator.requests[3])
        assert "Nothing was deleted" in str(refused["error"])
        assert await user_exists(stores, user.user_id)
    finally:
        await clean_up(stores, user)


async def test_confirming_without_a_request_or_in_the_same_turn_deletes_nothing(
    stores: Stores,
) -> None:
    user = await save_something(stores, LEAVING_SENDER_ID)
    agent = stores.agent(
        [
            CONFIRM,
            final_answer("en", "Nothing to confirm."),
            tool_calls((REQUEST_DATA_DELETION_TOOL, {}), (CONFIRM_DATA_DELETION_TOOL, {})),
            final_answer("en", "…"),
        ]
    )

    try:
        await answer(stores, agent, user, "yes")
        await answer(stores, agent, user, "delete my data, yes I'm sure")

        (no_request,) = tool_results(agent.generator.requests[1])
        _, same_turn = tool_results(agent.generator.requests[3])
        assert "Nothing was deleted" in str(no_request["error"])
        assert "Nothing was deleted" in str(same_turn["error"])
        assert await user_exists(stores, user.user_id)
    finally:
        await clean_up(stores, user)


async def test_another_users_request_never_lets_this_user_delete(stores: Stores) -> None:
    asking = await save_something(stores, LEAVING_SENDER_ID)
    other = await save_something(stores, STAYING_SENDER_ID)
    agent = stores.agent(
        [
            REQUEST,
            final_answer("en", "…"),
            CONFIRM,
            final_answer("en", "Nothing to confirm."),
            tool_calls((CONFIRM_DATA_DELETION_TOOL, {"user_id": str(asking.user_id)})),
            final_answer("en", "Nothing to confirm."),
        ]
    )

    try:
        await answer(stores, agent, asking, "delete my data")
        await answer(stores, agent, other, "yes")
        await answer(stores, agent, other, "yes, delete them")

        (refused,) = tool_results(agent.generator.requests[3])
        (with_user_id,) = tool_results(agent.generator.requests[5])
        assert "Nothing was deleted" in str(refused["error"])
        assert "user_id" in str(with_user_id["error"])
        assert await user_exists(stores, asking.user_id)
        assert await user_exists(stores, other.user_id)
        # The asking user's own request is still waiting for their own next message.
        assert await stores.requests.take(asking.user_id, stores.clock.now)
    finally:
        await clean_up(stores, asking, other)


async def test_a_failing_store_leaves_postgres_unchanged_and_a_repeat_succeeds(
    stores: Stores,
) -> None:
    user = await save_something(stores, LEAVING_SENDER_ID)

    try:
        failing = UserDataEraser(stores.sessions, stores.outside_postgres(FailingMediaStore()))
        with pytest.raises(OSError, match="synthetic media store failure"):
            await failing.erase(user.user_id)

        assert await user_exists(stores, user.user_id)
        async with stores.sessions() as session:
            assert await session.get(SavedItem, user.item_id) is not None

        deleted = await UserDataEraser(stores.sessions, stores.outside_postgres()).erase(
            user.user_id
        )

        assert deleted == 1
        assert not await user_exists(stores, user.user_id)
        assert not (stores.media_root / str(user.user_id)).exists()
        assert str(user.user_id) not in await queued_job_users(stores.redis)
    finally:
        await clean_up(stores, user)


async def test_erasing_an_unknown_user_changes_nothing(stores: Stores) -> None:
    eraser = UserDataEraser(stores.sessions, stores.outside_postgres())

    assert await eraser.erase(uuid.uuid4()) is None


def logged_text(records: Sequence[logging.LogRecord]) -> str:
    """Every message and every field of the records, as one text."""
    return "\n".join(
        f"{record.getMessage()} {' '.join(str(value) for value in vars(record).values())}"
        for record in records
    )


async def test_deletion_logs_hold_only_events_and_counts(
    stores: Stores, caplog: pytest.LogCaptureFixture
) -> None:
    user = await save_something(stores, LEAVING_SENDER_ID)
    request_text = f"please erase everything {user.marker}"
    confirm_text = f"yes, I am sure {user.marker}"
    agent = stores.agent(
        [REQUEST, final_answer("en", "…"), CONFIRM, final_answer("en", "Deleted.")]
    )

    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            await answer(stores, agent, user, request_text)
            await answer(stores, agent, user, confirm_text)

        events = [record.getMessage() for record in caplog.records]
        assert "data_deletion_requested" in events
        assert "user_data_deleted" in events
        logged = logged_text(caplog.records)
        forbidden = {
            "user id": str(user.user_id),
            "user id (hex)": user.user_id.hex,
            "item id": str(user.item_id),
            "sender id": LEAVING_SENDER_ID,
            "message marker": user.marker,
            "request text": request_text,
            "confirmation text": confirm_text,
        }
        assert [name for name, value in forbidden.items() if value in logged] == []
    finally:
        await clean_up(stores, user)
