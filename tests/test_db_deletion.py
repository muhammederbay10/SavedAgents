"""Checks on "delete my data": what goes with a user and what the shared tables keep.

These tests need Postgres with PostGIS and pgvector. See `tests/database.py`.
All ids and texts are synthetic.
"""

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import URL, String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.deletion import delete_user_data
from app.db.models import (
    AliasStatus,
    ConversationMessage,
    EnrichmentRun,
    Entity,
    EntityAvailability,
    ItemEntity,
    PlatformAccount,
    SavedItem,
    User,
    Vocabulary,
    VocabularyAlias,
)
from tests.database import run_alembic
from tests.db_factories import (
    INSTAGRAM,
    OTHER_REEL_ID,
    SENDER_ID,
    add_user,
    count_rows,
    reel_saved_by,
)

THIRD_REEL_ID = "18000000000000203"
# Wording the privacy gate would refuse: it stays on the user's own item only.
PERSONAL_WORDING = "my cousin's garage tour"
STAYING_USERS_WORDING = "car reviews"

ITEM_COUNTS_BEFORE = {
    ("category", "car review"): 3,
    ("subtype", "sedan"): 1,
    ("tag", "bmw"): 2,
    ("cuisine", "korean"): 1,
    ("genre", "horror"): 1,
    ("genre", "thriller"): 5,
}
# The leaving user has two car reviews (one of them a sedan), one item tagged
# "bmw", one Korean place and one horror thriller.
ITEM_COUNTS_AFTER = {
    ("category", "car review"): 1,
    ("subtype", "sedan"): 0,
    ("tag", "bmw"): 1,
    ("cuisine", "korean"): 0,
    ("genre", "horror"): 0,
    ("genre", "thriller"): 4,
}


LOCK_WAIT_POLL_SECONDS = 0.05
LOCK_WAIT_ATTEMPTS = 200
BLOCKED_REQUEST_TIMEOUT_SECONDS = 10.0
BACKEND_WAIT_STATE = "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :backend_pid"


@dataclass(frozen=True)
class SavedWorld:
    """The ids of two users' data, one of whom is about to leave."""

    leaving_user_id: uuid.UUID
    staying_user_id: uuid.UUID
    staying_item_id: uuid.UUID
    movie_id: uuid.UUID


async def add_vocabulary(session: AsyncSession, item_counts: dict[tuple[str, str], int]) -> None:
    session.add_all(
        Vocabulary(field=field, value=value, item_count=item_count)
        for (field, value), item_count in item_counts.items()
    )
    await session.flush()


async def item_counts(session: AsyncSession) -> dict[tuple[str, str], int]:
    rows = await session.execute(select(Vocabulary.field, Vocabulary.value, Vocabulary.item_count))
    return {(field, value): item_count for field, value, item_count in rows}


async def add_saved_world(session: AsyncSession) -> SavedWorld:
    leaving_user = await add_user(session)
    staying_user = await add_user(session)
    await add_vocabulary(session, ITEM_COUNTS_BEFORE)
    korean = await session.execute(select(Vocabulary.id).where(Vocabulary.value == "korean"))
    movie = Entity(type="movie", source="tmdb", external_id="27205", name="Inception")
    leaving_items = [
        reel_saved_by(
            leaving_user,
            category="car review",
            subtype="sedan",
            tags=["bmw", "m3"],
            raw_values={"category": {"raw": "reseña de coches", "set_by_user": False}},
        ),
        reel_saved_by(
            leaving_user,
            platform_post_id=OTHER_REEL_ID,
            category="car review",
            fields={"cuisine": "korean", "genre": ["horror", "thriller"], "year": 2021},
        ),
        reel_saved_by(
            leaving_user,
            platform_post_id=THIRD_REEL_ID,
            category=PERSONAL_WORDING,
            raw_values={"category": {"raw": PERSONAL_WORDING, "set_by_user": True}},
        ),
    ]
    staying_item = reel_saved_by(
        staying_user,
        category="car review",
        tags=["bmw"],
        raw_values={"category": {"raw": STAYING_USERS_WORDING, "set_by_user": False}},
    )
    session.add_all(
        [
            movie,
            *leaving_items,
            staying_item,
            VocabularyAlias(
                vocabulary_id=korean.scalar_one(),
                alias="comida coreana",
                status=AliasStatus.VERIFIED,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            *(
                EnrichmentRun(
                    saved_item_id=item.id,
                    step="normalize",
                    status="done",
                    output={"raw": item.raw_values},
                )
                for item in [*leaving_items, staying_item]
            ),
            PlatformAccount(
                user_id=leaving_user.id, platform=INSTAGRAM, platform_user_id=SENDER_ID
            ),
            ConversationMessage(user_id=leaving_user.id, role="user", content="Test"),
            ItemEntity(saved_item_id=leaving_items[1].id, entity_id=movie.id),
            EntityAvailability(entity_id=movie.id, country="US", providers={"flatrate": ["Max"]}),
            EntityAvailability(entity_id=movie.id, country="JP"),
        ]
    )
    await session.flush()
    return SavedWorld(
        leaving_user_id=leaving_user.id,
        staying_user_id=staying_user.id,
        staying_item_id=staying_item.id,
        movie_id=movie.id,
    )


async def test_delete_my_data_removes_the_users_items_raw_wording_and_enrichment_runs(
    db_session: AsyncSession,
) -> None:
    world = await add_saved_world(db_session)

    await delete_user_data(db_session, world.leaving_user_id)

    assert await count_rows(db_session, User, User.id == world.leaving_user_id) == 0
    assert await count_rows(db_session, SavedItem, SavedItem.user_id == world.leaving_user_id) == 0
    assert await count_rows(db_session, PlatformAccount) == 0
    assert await count_rows(db_session, ConversationMessage) == 0
    assert await count_rows(db_session, ItemEntity) == 0
    remaining_runs = await db_session.execute(select(EnrichmentRun.saved_item_id))
    assert remaining_runs.scalars().all() == [world.staying_item_id]
    for table_with_wording, wording_column in (
        (SavedItem, SavedItem.raw_values),
        (SavedItem, SavedItem.category),
        (EnrichmentRun, EnrichmentRun.output),
    ):
        still_holds_wording = cast(wording_column, String).contains(PERSONAL_WORDING)
        assert await count_rows(db_session, table_with_wording, still_holds_wording) == 0


async def test_delete_my_data_lowers_each_item_count_by_the_users_items(
    db_session: AsyncSession,
) -> None:
    world = await add_saved_world(db_session)

    await delete_user_data(db_session, world.leaving_user_id)

    assert await item_counts(db_session) == ITEM_COUNTS_AFTER


async def test_delete_my_data_keeps_other_users_items_and_the_shared_records(
    db_session: AsyncSession,
) -> None:
    world = await add_saved_world(db_session)

    await delete_user_data(db_session, world.leaving_user_id)

    staying_items = await db_session.execute(
        select(SavedItem.id, SavedItem.raw_values).where(SavedItem.user_id == world.staying_user_id)
    )
    assert staying_items.all() == [
        (world.staying_item_id, {"category": {"raw": STAYING_USERS_WORDING, "set_by_user": False}})
    ]
    assert await count_rows(db_session, User, User.id == world.staying_user_id) == 1
    assert await count_rows(db_session, Vocabulary) == len(ITEM_COUNTS_BEFORE)
    aliases = await db_session.execute(select(VocabularyAlias.alias, VocabularyAlias.status))
    assert aliases.all() == [("comida coreana", AliasStatus.VERIFIED)]
    assert await count_rows(db_session, Entity, Entity.id == world.movie_id) == 1
    availability = await db_session.execute(
        select(EntityAvailability.country, EntityAvailability.providers).order_by(
            EntityAvailability.country
        )
    )
    assert availability.all() == [("JP", {}), ("US", {"flatrate": ["Max"]})]


async def test_delete_my_data_counts_an_item_once_and_never_goes_below_zero(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    await add_vocabulary(db_session, {("tag", "bmw"): 5, ("category", "car review"): 1})
    db_session.add_all(
        [
            reel_saved_by(user, category="car review", tags=["bmw", "bmw"]),
            reel_saved_by(user, platform_post_id=OTHER_REEL_ID, category="car review"),
        ]
    )
    await db_session.flush()

    await delete_user_data(db_session, user.id)

    assert await item_counts(db_session) == {("tag", "bmw"): 4, ("category", "car review"): 0}


async def test_delete_my_data_for_an_unknown_user_changes_nothing(
    db_session: AsyncSession,
) -> None:
    world = await add_saved_world(db_session)

    await delete_user_data(db_session, uuid.uuid4())

    assert await item_counts(db_session) == ITEM_COUNTS_BEFORE
    assert await count_rows(db_session, User) == 2
    assert await count_rows(db_session, SavedItem, SavedItem.user_id == world.leaving_user_id) == 3


# --- two requests at the same time -----------------------------------------------------


@pytest.fixture
def committed_database_url(empty_database_url: URL, tmp_path: Path) -> URL:
    """A migrated database of its own, for a test whose sessions really commit."""
    run_alembic(empty_database_url, tmp_path, "upgrade", "head")
    return empty_database_url


async def wait_until_blocked_on_a_lock(observer: AsyncConnection, backend_pid: int) -> None:
    """Return once that database session is waiting for another session's lock."""
    for _ in range(LOCK_WAIT_ATTEMPTS):
        wait_state = await observer.execute(text(BACKEND_WAIT_STATE), {"backend_pid": backend_pid})
        if wait_state.scalar_one_or_none() == "Lock":
            return
        await asyncio.sleep(LOCK_WAIT_POLL_SECONDS)
    raise AssertionError("the second deletion request never waited for the first one")


async def test_two_delete_requests_for_one_user_lower_the_item_count_only_once(
    committed_database_url: URL,
) -> None:
    engine = create_async_engine(committed_database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            leaving_user = await add_user(setup)
            staying_user = await add_user(setup)
            await add_vocabulary(setup, {("category", "car review"): 2})
            setup.add_all(
                [
                    reel_saved_by(leaving_user, category="car review"),
                    reel_saved_by(staying_user, category="car review"),
                ]
            )
            await setup.commit()

        async with (
            AsyncSession(engine) as first_request,
            AsyncSession(engine) as second_request,
            engine.connect() as connection,
        ):
            # Each statement must see the other sessions as they are right now.
            observer = await connection.execution_options(isolation_level="AUTOCOMMIT")
            second_pid = await second_request.scalar(text("SELECT pg_backend_pid()"))
            assert second_pid is not None

            await delete_user_data(first_request, leaving_user.id)
            overlapping_delete = asyncio.create_task(
                delete_user_data(second_request, leaving_user.id)
            )
            try:
                await wait_until_blocked_on_a_lock(observer, second_pid)
            finally:
                await first_request.commit()
            await asyncio.wait_for(overlapping_delete, BLOCKED_REQUEST_TIMEOUT_SECONDS)
            await second_request.commit()

        async with AsyncSession(engine) as check:
            assert await item_counts(check) == {("category", "car review"): 1}
            assert await count_rows(check, SavedItem, SavedItem.user_id == staying_user.id) == 1
            assert await count_rows(check, User, User.id == leaving_user.id) == 0
            assert await count_rows(check, SavedItem, SavedItem.user_id == leaving_user.id) == 0
    finally:
        await engine.dispose()
