"""Checks on the tables all users share, and on the wording an item keeps for itself.

These tests need Postgres with PostGIS and pgvector. See `tests/database.py`.
All ids and texts are synthetic.

The JSON shapes used here for `raw_values`, for the normalize step's output and
for `providers` are examples. The pipeline and the resolvers (SAV-14, SAV-15)
define them. The schema only has to store them and let them be queried.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AliasStatus,
    EnrichmentRun,
    Entity,
    EntityAvailability,
    SavedItem,
    Vocabulary,
    VocabularyAlias,
)
from tests.database import TEST_EMBEDDING_DIMENSION
from tests.db_factories import OTHER_REEL_ID, add_user, count_rows, reel_saved_by, unit_vector

VOCABULARY_COLUMNS = {"id", "field", "value", "embedding", "item_count", "created_at"}
NORMALIZE_STEP = "normalize"

UNITED_STATES = "US"
JAPAN = "JP"
US_PROVIDERS = {"flatrate": ["Netflix"], "rent": ["Apple TV"]}
JAPAN_PROVIDERS = {"flatrate": ["U-NEXT"]}
REFRESHED_US_PROVIDERS = {"flatrate": ["Max"]}
US_FIRST_FETCHED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
JAPAN_FIRST_FETCHED = datetime(2026, 10, 20, 15, 30, tzinfo=UTC)
US_REFRESHED = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


async def add_value(session: AsyncSession, field: str, value: str) -> Vocabulary:
    vocabulary_value = Vocabulary(field=field, value=value)
    session.add(vocabulary_value)
    await session.flush()
    return vocabulary_value


async def add_movie(session: AsyncSession) -> Entity:
    movie = Entity(
        type="movie",
        source="tmdb",
        external_id="27205",
        name="Inception",
        data={"genres": ["sci-fi"], "runtime": 148},
    )
    session.add(movie)
    await session.flush()
    return movie


# --- vocabulary --------------------------------------------------------------------


async def test_duplicate_vocabulary_value_in_one_field_is_rejected(
    db_session: AsyncSession,
) -> None:
    await add_value(db_session, "cuisine", "korean")

    db_session.add(Vocabulary(field="cuisine", value="korean"))

    with pytest.raises(IntegrityError, match="uq_vocabulary_field_value"):
        await db_session.flush()


async def test_same_vocabulary_value_can_exist_in_two_fields(db_session: AsyncSession) -> None:
    await add_value(db_session, "cuisine", "korean")
    await add_value(db_session, "tag", "korean")

    assert await count_rows(db_session, Vocabulary, Vocabulary.value == "korean") == 2


async def test_new_vocabulary_value_starts_with_no_items(db_session: AsyncSession) -> None:
    value = await add_value(db_session, "category", "car review")

    await db_session.refresh(value)

    assert value.item_count == 0
    assert value.embedding is None
    assert value.created_at is not None


async def test_vocabulary_value_keeps_its_embedding_and_item_count(
    db_session: AsyncSession,
) -> None:
    value = Vocabulary(field="cuisine", value="korean", embedding=unit_vector(0), item_count=3)
    db_session.add(value)
    await db_session.flush()

    await db_session.refresh(value)

    assert value.embedding == unit_vector(0)
    assert value.item_count == 3


async def test_vocabulary_embedding_of_the_wrong_size_is_rejected(
    db_session: AsyncSession,
) -> None:
    db_session.add(Vocabulary(field="cuisine", value="korean", embedding=[1.0, 0.0, 0.0]))

    with pytest.raises(DBAPIError, match=f"expected {TEST_EMBEDDING_DIMENSION} dimensions"):
        await db_session.flush()


async def test_vocabulary_has_no_aliases_column(db_session: AsyncSession) -> None:
    columns = await db_session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'vocabulary'")
    )

    assert set(columns.scalars()) == VOCABULARY_COLUMNS


# --- vocabulary_alias ----------------------------------------------------------------


async def test_new_alias_starts_as_a_candidate_with_no_corrections(
    db_session: AsyncSession,
) -> None:
    korean = await add_value(db_session, "cuisine", "korean")
    alias = VocabularyAlias(vocabulary_id=korean.id, alias="comida coreana", probability=0.93)
    db_session.add(alias)
    await db_session.flush()

    await db_session.refresh(alias)

    assert alias.status == AliasStatus.CANDIDATE
    assert alias.probability == pytest.approx(0.93)
    assert alias.correction_count == 0
    assert alias.created_at is not None


async def test_alias_keeps_its_counts_through_verified_and_rejected(
    db_session: AsyncSession,
) -> None:
    korean = await add_value(db_session, "cuisine", "korean")
    alias = VocabularyAlias(vocabulary_id=korean.id, alias="한식", probability=0.81)
    db_session.add(alias)
    await db_session.flush()

    alias.status = AliasStatus.VERIFIED
    alias.probability = 0.97
    await db_session.flush()
    await db_session.refresh(alias)
    verified = (alias.status, alias.probability, alias.correction_count)

    alias.status = AliasStatus.REJECTED
    alias.correction_count = 4
    await db_session.flush()
    await db_session.refresh(alias)
    rejected = (alias.status, alias.probability, alias.correction_count)

    assert verified == (AliasStatus.VERIFIED, pytest.approx(0.97), 0)
    assert rejected == (AliasStatus.REJECTED, pytest.approx(0.97), 4)


async def test_unknown_alias_status_is_rejected(db_session: AsyncSession) -> None:
    korean = await add_value(db_session, "cuisine", "korean")
    db_session.add(VocabularyAlias(vocabulary_id=korean.id, alias="Korean food", status="maybe"))

    with pytest.raises(IntegrityError, match="ck_vocabulary_alias_status_is_known"):
        await db_session.flush()


async def test_same_alias_twice_for_one_value_is_rejected(db_session: AsyncSession) -> None:
    korean = await add_value(db_session, "cuisine", "korean")
    db_session.add(VocabularyAlias(vocabulary_id=korean.id, alias="Korean food"))
    await db_session.flush()

    db_session.add(VocabularyAlias(vocabulary_id=korean.id, alias="Korean food"))

    with pytest.raises(IntegrityError, match="uq_vocabulary_alias_vocabulary_id_alias"):
        await db_session.flush()


async def test_exact_hit_finds_only_a_verified_alias_of_the_same_field(
    db_session: AsyncSession,
) -> None:
    korean_cuisine = await add_value(db_session, "cuisine", "korean")
    korean_bbq = await add_value(db_session, "cuisine", "korean bbq")
    korean_tag = await add_value(db_session, "tag", "korean")
    wording = "kfood"
    db_session.add_all(
        [
            VocabularyAlias(
                vocabulary_id=korean_cuisine.id, alias=wording, status=AliasStatus.VERIFIED
            ),
            VocabularyAlias(
                vocabulary_id=korean_bbq.id, alias=wording, status=AliasStatus.CANDIDATE
            ),
            VocabularyAlias(
                vocabulary_id=korean_tag.id, alias=wording, status=AliasStatus.REJECTED
            ),
        ]
    )
    await db_session.flush()

    hits = await db_session.execute(
        select(Vocabulary.id)
        .join(VocabularyAlias, VocabularyAlias.vocabulary_id == Vocabulary.id)
        .where(
            Vocabulary.field == "cuisine",
            VocabularyAlias.alias == wording,
            VocabularyAlias.status == AliasStatus.VERIFIED,
        )
    )

    assert hits.scalars().all() == [korean_cuisine.id]


async def test_cleaning_up_a_vocabulary_value_removes_its_aliases(
    db_session: AsyncSession,
) -> None:
    unused = await add_value(db_session, "cuisine", "korean")
    kept = await add_value(db_session, "cuisine", "thai")
    db_session.add_all(
        [
            VocabularyAlias(vocabulary_id=unused.id, alias="Korean food"),
            VocabularyAlias(vocabulary_id=kept.id, alias="Thai food"),
        ]
    )
    await db_session.flush()

    await db_session.execute(delete(Vocabulary).where(Vocabulary.id == unused.id))

    remaining = await db_session.execute(select(VocabularyAlias.alias))
    assert remaining.scalars().all() == ["Thai food"]


# --- saved_item.raw_values and the normalize step's provenance -------------------------


async def test_item_keeps_its_own_wording_and_hand_set_marks_next_to_the_values(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    raw_values = {
        "category": {"raw": "reseña de coches", "set_by_user": False},
        "tags": [
            {"raw": "BMW M3", "set_by_user": False},
            {"raw": "road trip", "set_by_user": True},
        ],
    }
    item = reel_saved_by(
        user, category="car review", tags=["bmw m3", "road trip"], raw_values=raw_values
    )
    db_session.add(item)
    await db_session.flush()

    await db_session.refresh(item)
    with_hand_set_tag = await db_session.execute(
        select(SavedItem.id).where(SavedItem.raw_values.contains({"tags": [{"set_by_user": True}]}))
    )

    assert item.raw_values == raw_values
    assert (item.category, item.tags) == ("car review", ["bmw m3", "road trip"])
    assert with_hand_set_tag.scalars().all() == [item.id]


async def test_items_matched_through_an_alias_are_found_with_their_raw_wording(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    car_review = await add_value(db_session, "category", "car review")
    alias = VocabularyAlias(vocabulary_id=car_review.id, alias="reseña de coches")
    matched_by_alias = reel_saved_by(
        user,
        category="car review",
        raw_values={"category": {"raw": "reseña de coches", "set_by_user": False}},
    )
    exact_hit = reel_saved_by(
        user,
        platform_post_id=OTHER_REEL_ID,
        category="car review",
        raw_values={"category": {"raw": "car review", "set_by_user": False}},
    )
    db_session.add_all([alias, matched_by_alias, exact_hit])
    await db_session.flush()
    match_through_alias = {
        "field": "category",
        "value": "car review",
        "alias_id": str(alias.id),
        "probability": 0.93,
    }
    db_session.add_all(
        [
            EnrichmentRun(
                saved_item_id=matched_by_alias.id,
                step=NORMALIZE_STEP,
                status="done",
                output={"matches": [match_through_alias]},
            ),
            EnrichmentRun(
                saved_item_id=exact_hit.id,
                step=NORMALIZE_STEP,
                status="done",
                output={
                    "matches": [{"field": "category", "value": "car review", "alias_id": None}]
                },
            ),
        ]
    )
    await db_session.flush()

    to_normalize_again = await db_session.execute(
        select(SavedItem.id, SavedItem.raw_values)
        .join(EnrichmentRun, EnrichmentRun.saved_item_id == SavedItem.id)
        .where(
            EnrichmentRun.step == NORMALIZE_STEP,
            EnrichmentRun.output.contains({"matches": [{"alias_id": str(alias.id)}]}),
        )
    )

    assert to_normalize_again.all() == [
        (matched_by_alias.id, {"category": {"raw": "reseña de coches", "set_by_user": False}})
    ]


# --- entity_availability ---------------------------------------------------------------


async def test_refreshing_one_country_leaves_the_other_countrys_providers_and_freshness(
    db_session: AsyncSession,
) -> None:
    movie = await add_movie(db_session)
    db_session.add_all(
        [
            EntityAvailability(
                entity_id=movie.id,
                country=UNITED_STATES,
                providers=US_PROVIDERS,
                refreshed_at=US_FIRST_FETCHED,
            ),
            EntityAvailability(
                entity_id=movie.id,
                country=JAPAN,
                providers=JAPAN_PROVIDERS,
                refreshed_at=JAPAN_FIRST_FETCHED,
            ),
        ]
    )
    await db_session.flush()

    await db_session.execute(
        update(EntityAvailability)
        .where(
            EntityAvailability.entity_id == movie.id, EntityAvailability.country == UNITED_STATES
        )
        .values(providers=REFRESHED_US_PROVIDERS, refreshed_at=US_REFRESHED)
    )

    rows = await db_session.execute(
        select(
            EntityAvailability.country,
            EntityAvailability.providers,
            EntityAvailability.refreshed_at,
        ).where(EntityAvailability.entity_id == movie.id)
    )
    by_country = {country: (providers, refreshed_at) for country, providers, refreshed_at in rows}
    movie_data = await db_session.execute(select(Entity.data).where(Entity.id == movie.id))
    assert by_country == {
        UNITED_STATES: (REFRESHED_US_PROVIDERS, US_REFRESHED),
        JAPAN: (JAPAN_PROVIDERS, JAPAN_FIRST_FETCHED),
    }
    assert movie_data.scalar_one() == {"genres": ["sci-fi"], "runtime": 148}


async def test_second_availability_row_for_one_entity_and_country_is_rejected(
    db_session: AsyncSession,
) -> None:
    movie = await add_movie(db_session)
    db_session.add(EntityAvailability(entity_id=movie.id, country=JAPAN))
    await db_session.flush()

    with pytest.raises(IntegrityError, match="pk_entity_availability"):
        await db_session.execute(
            insert(EntityAvailability).values(entity_id=movie.id, country=JAPAN)
        )


async def test_new_availability_row_has_no_providers_and_its_own_refresh_time(
    db_session: AsyncSession,
) -> None:
    movie = await add_movie(db_session)
    availability = EntityAvailability(entity_id=movie.id, country=JAPAN)
    db_session.add(availability)
    await db_session.flush()

    await db_session.refresh(availability)

    assert availability.providers == {}
    assert availability.refreshed_at is not None


async def test_deleting_an_entity_deletes_its_availability(db_session: AsyncSession) -> None:
    movie = await add_movie(db_session)
    db_session.add(EntityAvailability(entity_id=movie.id, country=JAPAN))
    await db_session.flush()

    await db_session.execute(delete(Entity).where(Entity.id == movie.id))

    assert await count_rows(db_session, EntityAvailability) == 0
