"""Checks on what the migrated schema accepts, rejects and can answer.

These tests need Postgres with PostGIS and pgvector. See `tests/database.py`.
All ids and texts are synthetic.
"""

import math
import uuid

import pytest
from sqlalchemy import ColumnElement, Select, func, literal, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FULL_TEXT_SEARCH_CONFIG,
    Base,
    ConversationMessage,
    EnrichmentRun,
    Entity,
    EntityStatus,
    ItemEntity,
    ItemStatus,
    PlatformAccount,
    SavedItem,
    User,
    UserEntity,
    Vocabulary,
    saved_item_search_vector,
)
from app.db.types import GeographyPoint, GeoPoint
from tests.database import TEST_EMBEDDING_DIMENSION

INSTAGRAM = "instagram"
REEL_ID = "18000000000000201"
OTHER_REEL_ID = "18000000000000202"
REEL_URL = "https://www.instagram.com/reel/FAKEREEL002/"
OTHER_REEL_URL = "https://www.instagram.com/reel/FAKEREEL006/"
MESSAGE_ID = "aWdfZAGFAKEMID0002"
OTHER_MESSAGE_ID = "aWdfZAGFAKEMID0008"
SENDER_ID = "1000000000000001"

BESIKTAS_PIER = GeoPoint(latitude=41.0410, longitude=29.0070)
MODA_PIER = GeoPoint(latitude=40.9790, longitude=29.0250)
# About 280 m from the Beşiktaş pier and about 7.3 km from the Moda pier.
STANDING_IN_BESIKTAS = GeoPoint(latitude=41.0430, longitude=29.0050)
SEARCH_RADIUS_METERS = 1000
EARTH_RADIUS_METERS = 6_371_008.8
# PostGIS measures on the WGS 84 spheroid, the haversine check below on a sphere.
SPHERE_VERSUS_SPHEROID_TOLERANCE = 0.01

EXPECTED_INDEX_DEFINITIONS = {
    "ix_entity_location": "USING gist (location)",
    "ix_entity_data": "USING gin (data)",
    "ix_saved_item_fields": "USING gin (fields)",
    "ix_saved_item_embedding": "USING hnsw (embedding vector_cosine_ops)",
    "ix_saved_item_full_text": "USING gin (to_tsvector(",
    "uq_saved_item_user_id_source_message_id": (
        "USING btree (user_id, source_message_id) WHERE (source_message_id IS NOT NULL)"
    ),
}


async def add_user(session: AsyncSession) -> User:
    user = User(display_name="Test User")
    session.add(user)
    await session.flush()
    return user


def reel_saved_by(user: User, **overrides: object) -> SavedItem:
    values: dict[str, object] = {
        "user_id": user.id,
        "platform": INSTAGRAM,
        "platform_post_id": REEL_ID,
        "post_url": REEL_URL,
        "media_type": "reel",
    }
    return SavedItem(**{**values, **overrides})


def collection_save_by(user: User, message_id: str) -> SavedItem:
    """A reel saved into a collaborative collection: a message id, no post id and no link."""
    return SavedItem(
        user_id=user.id, platform=INSTAGRAM, media_type="reel", source_message_id=message_id
    )


def place(external_id: str, name: str, location: GeoPoint | None = None) -> Entity:
    return Entity(
        type="place",
        source="google_places",
        external_id=external_id,
        name=name,
        location=location,
    )


def unit_vector(axis: int) -> list[float]:
    return [1.0 if index == axis else 0.0 for index in range(TEST_EMBEDDING_DIMENSION)]


def haversine_meters(first: GeoPoint, second: GeoPoint) -> float:
    first_latitude, second_latitude = math.radians(first.latitude), math.radians(second.latitude)
    latitude_delta = second_latitude - first_latitude
    longitude_delta = math.radians(second.longitude - first.longitude)
    half_chord = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude) * math.cos(second_latitude) * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(half_chord))


async def count_rows(
    session: AsyncSession, model: type[Base], *conditions: ColumnElement[bool]
) -> int:
    statement = select(func.count()).select_from(model).where(*conditions)
    return (await session.execute(statement)).scalar_one()


async def query_plan_without_sequential_scans(
    session: AsyncSession, statement: Select[tuple[uuid.UUID]]
) -> str:
    """Return the plan Postgres picks when it may not simply read the whole table."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    sql = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    plan = await session.execute(text(f"EXPLAIN {sql}"))
    return "\n".join(row[0] for row in plan)


# --- saved_item: dedupe keys (decision record 0007) ----------------------------


async def test_duplicate_share_of_a_post_by_one_user_is_rejected(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(reel_saved_by(user))
    await db_session.flush()

    db_session.add(reel_saved_by(user))

    with pytest.raises(IntegrityError, match="uq_saved_item_user_id_platform_platform_post_id"):
        await db_session.flush()


async def test_two_users_can_save_the_same_post(db_session: AsyncSession) -> None:
    first_user = await add_user(db_session)
    second_user = await add_user(db_session)

    db_session.add_all([reel_saved_by(first_user), reel_saved_by(second_user)])
    await db_session.flush()

    assert await count_rows(db_session, SavedItem, SavedItem.platform_post_id == REEL_ID) == 2


async def test_same_post_id_on_another_platform_is_a_different_item(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)

    db_session.add_all([reel_saved_by(user), reel_saved_by(user, platform="tiktok")])
    await db_session.flush()

    assert await count_rows(db_session, SavedItem, SavedItem.user_id == user.id) == 2


async def test_same_message_stored_twice_for_one_user_is_rejected(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    db_session.add(collection_save_by(user, MESSAGE_ID))
    await db_session.flush()

    db_session.add(collection_save_by(user, MESSAGE_ID))

    with pytest.raises(IntegrityError, match="uq_saved_item_user_id_source_message_id"):
        await db_session.flush()


async def test_same_message_is_rejected_even_when_the_post_ids_differ(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    db_session.add(reel_saved_by(user, source_message_id=MESSAGE_ID))
    await db_session.flush()

    db_session.add(
        reel_saved_by(user, platform_post_id=OTHER_REEL_ID, source_message_id=MESSAGE_ID)
    )

    with pytest.raises(IntegrityError, match="uq_saved_item_user_id_source_message_id"):
        await db_session.flush()


async def test_two_users_can_have_the_same_message_id(db_session: AsyncSession) -> None:
    first_user = await add_user(db_session)
    second_user = await add_user(db_session)

    db_session.add_all(
        [collection_save_by(first_user, MESSAGE_ID), collection_save_by(second_user, MESSAGE_ID)]
    )
    await db_session.flush()

    assert await count_rows(db_session, SavedItem, SavedItem.source_message_id == MESSAGE_ID) == 2


async def test_same_reel_saved_through_two_messages_is_stored_twice(
    db_session: AsyncSession,
) -> None:
    # The accepted limit of decision record 0007: without a post id the keys tell
    # messages apart, not reels, and nothing in the payload links the two saves.
    user = await add_user(db_session)

    db_session.add_all(
        [collection_save_by(user, MESSAGE_ID), collection_save_by(user, OTHER_MESSAGE_ID)]
    )
    await db_session.flush()

    assert await count_rows(db_session, SavedItem, SavedItem.user_id == user.id) == 2


async def test_items_with_only_a_post_link_are_accepted_and_do_not_collide(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)

    db_session.add_all(
        [
            SavedItem(user_id=user.id, platform=INSTAGRAM, post_url=REEL_URL),
            SavedItem(user_id=user.id, platform=INSTAGRAM, post_url=OTHER_REEL_URL),
        ]
    )
    await db_session.flush()

    assert await count_rows(db_session, SavedItem, SavedItem.user_id == user.id) == 2


async def test_item_without_post_id_message_id_or_post_link_is_rejected(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    db_session.add(SavedItem(user_id=user.id, platform=INSTAGRAM, caption="A caption"))

    with pytest.raises(IntegrityError, match="ck_saved_item_has_post_id_message_id_or_post_url"):
        await db_session.flush()


@pytest.mark.parametrize("column", ["platform_post_id", "source_message_id", "post_url"])
async def test_empty_text_is_rejected_where_a_missing_value_must_be_null(
    db_session: AsyncSession, column: str
) -> None:
    user = await add_user(db_session)
    db_session.add(reel_saved_by(user, **{column: ""}))

    with pytest.raises(IntegrityError, match=f"ck_saved_item_{column}_is_not_empty"):
        await db_session.flush()


# --- saved_item: defaults and status -------------------------------------------


async def test_new_item_starts_pending_with_empty_tags_and_fields(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    item = reel_saved_by(user)
    db_session.add(item)
    await db_session.flush()

    await db_session.refresh(item)

    assert item.status == ItemStatus.PENDING
    assert item.tags == []
    assert item.fields == {}
    assert item.attributes == {}
    assert item.saved_at is not None
    assert item.source_message_id is None


async def test_item_keeps_an_open_category_without_a_built_in_type(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    item = reel_saved_by(user, category="car review", tags=["bmw", "m3"])
    db_session.add(item)
    await db_session.flush()

    await db_session.refresh(item)

    assert item.category == "car review"
    assert item.type is None
    assert item.tags == ["bmw", "m3"]


async def test_unknown_item_status_is_rejected(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(reel_saved_by(user, status="almost_done"))

    with pytest.raises(IntegrityError, match="ck_saved_item_status_is_known"):
        await db_session.flush()


# --- saved_item: source_message_id (decision record 0004) ----------------------


async def test_item_is_found_by_user_and_source_message_id(db_session: AsyncSession) -> None:
    owner = await add_user(db_session)
    other_user = await add_user(db_session)
    owners_item = reel_saved_by(owner, source_message_id=MESSAGE_ID)
    db_session.add_all(
        [
            owners_item,
            reel_saved_by(owner, platform_post_id=OTHER_REEL_ID, source_message_id=None),
            reel_saved_by(other_user, source_message_id=OTHER_MESSAGE_ID),
        ]
    )
    await db_session.flush()

    found = await db_session.execute(
        select(SavedItem.id).where(
            SavedItem.user_id == owner.id, SavedItem.source_message_id == MESSAGE_ID
        )
    )

    assert found.scalars().all() == [owners_item.id]


async def test_deleting_a_user_deletes_their_items_and_message_ids(
    db_session: AsyncSession,
) -> None:
    leaving_user = await add_user(db_session)
    staying_user = await add_user(db_session)
    restaurant = place("ChIJ-fake-place-1", "Moda Meyhanesi")
    leaving_item = reel_saved_by(leaving_user, source_message_id=MESSAGE_ID)
    staying_item = reel_saved_by(staying_user, source_message_id=OTHER_MESSAGE_ID)
    db_session.add_all([restaurant, leaving_item, staying_item])
    await db_session.flush()
    db_session.add_all(
        [
            PlatformAccount(
                user_id=leaving_user.id, platform=INSTAGRAM, platform_user_id=SENDER_ID
            ),
            ItemEntity(saved_item_id=leaving_item.id, entity_id=restaurant.id),
            EnrichmentRun(saved_item_id=leaving_item.id, step="classify", status="done"),
            UserEntity(user_id=leaving_user.id, entity_id=restaurant.id, status=EntityStatus.WANT),
            ConversationMessage(user_id=leaving_user.id, role="user", content="Test"),
        ]
    )
    await db_session.flush()

    await db_session.delete(leaving_user)
    await db_session.flush()

    assert await count_rows(db_session, SavedItem, SavedItem.source_message_id == MESSAGE_ID) == 0
    for model_with_user_data in (
        PlatformAccount,
        ItemEntity,
        EnrichmentRun,
        UserEntity,
        ConversationMessage,
    ):
        assert await count_rows(db_session, model_with_user_data) == 0
    assert await count_rows(db_session, SavedItem, SavedItem.user_id == staying_user.id) == 1
    assert await count_rows(db_session, Entity, Entity.id == restaurant.id) == 1


# --- saved_item: vector and keyword search --------------------------------------


async def test_items_are_ranked_by_embedding_similarity(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    similar = reel_saved_by(user, embedding=unit_vector(0))
    different = reel_saved_by(user, platform_post_id=OTHER_REEL_ID, embedding=unit_vector(1))
    db_session.add_all([different, similar])
    await db_session.flush()
    question = [0.9, 0.1, *([0.0] * (TEST_EMBEDDING_DIMENSION - 2))]

    ranked = await db_session.execute(
        select(SavedItem.id, SavedItem.embedding)
        .where(SavedItem.user_id == user.id)
        .order_by(SavedItem.embedding.op("<=>")(question))
    )

    assert ranked.all() == [(similar.id, unit_vector(0)), (different.id, unit_vector(1))]


async def test_embedding_of_the_wrong_size_is_rejected(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add(reel_saved_by(user, embedding=[1.0, 0.0, 0.0]))

    with pytest.raises(DBAPIError, match=f"expected {TEST_EMBEDDING_DIMENSION} dimensions"):
        await db_session.flush()


@pytest.mark.parametrize("searched_column", ["caption", "summary", "transcript"])
async def test_keyword_search_finds_a_word_in_caption_summary_or_transcript(
    db_session: AsyncSession, searched_column: str
) -> None:
    user = await add_user(db_session)
    matching = reel_saved_by(user, **{searched_column: "Moda'da küçük bir meyhane"})
    unrelated = reel_saved_by(user, platform_post_id=OTHER_REEL_ID, caption="İntegral çözümü")
    db_session.add_all([matching, unrelated])
    await db_session.flush()
    keyword = func.plainto_tsquery(FULL_TEXT_SEARCH_CONFIG, "MEYHANE")

    found = await db_session.execute(
        select(SavedItem.id).where(saved_item_search_vector().op("@@")(keyword))
    )

    assert found.scalars().all() == [matching.id]


async def test_keyword_search_can_use_the_full_text_index(db_session: AsyncSession) -> None:
    keyword = text(f"plainto_tsquery('{FULL_TEXT_SEARCH_CONFIG}', 'meyhane')")
    statement = select(SavedItem.id).where(saved_item_search_vector().op("@@")(keyword))

    plan = await query_plan_without_sequential_scans(db_session, statement)

    assert "ix_saved_item_full_text" in plan


# --- entity ----------------------------------------------------------------------


async def test_duplicate_entity_from_one_source_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(place("ChIJ-fake-place-1", "Moda Meyhanesi"))
    await db_session.flush()

    db_session.add(place("ChIJ-fake-place-1", "Moda Meyhanesi (copy)"))

    with pytest.raises(IntegrityError, match="uq_entity_source_external_id"):
        await db_session.flush()


async def test_same_external_id_from_another_source_is_a_different_entity(
    db_session: AsyncSession,
) -> None:
    movie = Entity(type="movie", source="tmdb", external_id="27205", name="Inception")
    book = Entity(type="book", source="open_library", external_id="27205", name="A book")

    db_session.add_all([movie, book])
    await db_session.flush()

    assert await count_rows(db_session, Entity, Entity.external_id == "27205") == 2


async def test_places_are_found_by_distance_from_a_point(db_session: AsyncSession) -> None:
    near = place("ChIJ-fake-besiktas", "Beşiktaş pier", BESIKTAS_PIER)
    far = place("ChIJ-fake-moda", "Moda pier", MODA_PIER)
    movie = Entity(type="movie", source="tmdb", external_id="27205", name="Inception")
    db_session.add_all([near, far, movie])
    await db_session.flush()
    here = literal(STANDING_IN_BESIKTAS, GeographyPoint())

    within_radius = await db_session.execute(
        select(Entity.name).where(func.ST_DWithin(Entity.location, here, SEARCH_RADIUS_METERS))
    )
    distances = await db_session.execute(
        select(Entity.name, func.ST_Distance(Entity.location, here))
        .where(Entity.location.is_not(None))
        .order_by(func.ST_Distance(Entity.location, here))
    )

    assert within_radius.scalars().all() == ["Beşiktaş pier"]
    names_by_distance, meters = zip(*distances.all(), strict=True)
    assert names_by_distance == ("Beşiktaş pier", "Moda pier")
    assert meters == pytest.approx(
        [
            haversine_meters(STANDING_IN_BESIKTAS, BESIKTAS_PIER),
            haversine_meters(STANDING_IN_BESIKTAS, MODA_PIER),
        ],
        rel=SPHERE_VERSUS_SPHEROID_TOLERANCE,
    )


async def test_entity_location_is_read_back_as_the_point_that_was_stored(
    db_session: AsyncSession,
) -> None:
    pier = place("ChIJ-fake-besiktas", "Beşiktaş pier", BESIKTAS_PIER)
    db_session.add(pier)
    await db_session.flush()

    await db_session.refresh(pier)

    assert pier.location == BESIKTAS_PIER


async def test_place_keeps_every_address_level_and_is_found_by_any_of_them(
    db_session: AsyncSession,
) -> None:
    cafe = place("ChIJ-fake-brooklyn", "Brooklyn café")
    cafe.country = "United States"
    cafe.region = "New York"
    cafe.city = "New York"
    cafe.district = "Brooklyn"
    cafe.neighbourhood = "Williamsburg"
    pier_without_address = place("ChIJ-fake-besiktas", "Beşiktaş pier", BESIKTAS_PIER)
    db_session.add_all([cafe, pier_without_address])
    await db_session.flush()

    for address_level in (
        Entity.country == "United States",
        Entity.region == "New York",
        Entity.city == "New York",
        Entity.district == "Brooklyn",
        Entity.neighbourhood == "Williamsburg",
    ):
        found = await db_session.execute(select(Entity.name).where(address_level))
        assert found.scalars().all() == ["Brooklyn café"]


async def test_entities_are_found_by_a_value_inside_their_data(db_session: AsyncSession) -> None:
    horror = Entity(
        type="movie", source="tmdb", external_id="1", name="Horror", data={"genres": ["horror"]}
    )
    comedy = Entity(
        type="movie", source="tmdb", external_id="2", name="Comedy", data={"genres": ["comedy"]}
    )
    db_session.add_all([horror, comedy])
    await db_session.flush()

    found = await db_session.execute(
        select(Entity.name).where(Entity.data.contains({"genres": ["horror"]}))
    )

    assert found.scalars().all() == ["Horror"]


# --- vocabulary (decision record 0005) ---------------------------------------------


async def test_duplicate_vocabulary_value_in_one_field_is_rejected(
    db_session: AsyncSession,
) -> None:
    db_session.add(Vocabulary(field="cuisine", value="korean"))
    await db_session.flush()

    db_session.add(Vocabulary(field="cuisine", value="korean"))

    with pytest.raises(IntegrityError, match="uq_vocabulary_field_value"):
        await db_session.flush()


async def test_same_vocabulary_value_can_exist_in_two_fields(db_session: AsyncSession) -> None:
    db_session.add_all(
        [Vocabulary(field="cuisine", value="korean"), Vocabulary(field="tag", value="korean")]
    )
    await db_session.flush()

    assert await count_rows(db_session, Vocabulary, Vocabulary.value == "korean") == 2


async def test_new_vocabulary_value_starts_with_no_aliases_and_no_items(
    db_session: AsyncSession,
) -> None:
    value = Vocabulary(field="category", value="car review")
    db_session.add(value)
    await db_session.flush()

    await db_session.refresh(value)

    assert value.aliases == []
    assert value.item_count == 0
    assert value.embedding is None
    assert value.created_at is not None


async def test_vocabulary_value_keeps_aliases_in_any_language_and_its_embedding(
    db_session: AsyncSession,
) -> None:
    aliases = ["Korean food", "comida coreana", "한식"]
    value = Vocabulary(
        field="cuisine", value="korean", aliases=aliases, embedding=unit_vector(0), item_count=3
    )
    db_session.add(value)
    await db_session.flush()

    await db_session.refresh(value)

    assert value.aliases == aliases
    assert value.embedding == unit_vector(0)
    assert value.item_count == 3


async def test_vocabulary_embedding_of_the_wrong_size_is_rejected(
    db_session: AsyncSession,
) -> None:
    db_session.add(Vocabulary(field="cuisine", value="korean", embedding=[1.0, 0.0, 0.0]))

    with pytest.raises(DBAPIError, match=f"expected {TEST_EMBEDDING_DIMENSION} dimensions"):
        await db_session.flush()


async def test_deleting_a_user_keeps_the_shared_vocabulary(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    db_session.add_all(
        [
            reel_saved_by(user, category="car review"),
            Vocabulary(field="category", value="car review", item_count=1),
        ]
    )
    await db_session.flush()

    await db_session.delete(user)
    await db_session.flush()

    assert await count_rows(db_session, SavedItem, SavedItem.user_id == user.id) == 0
    assert await count_rows(db_session, Vocabulary, Vocabulary.value == "car review") == 1


# --- other tables ----------------------------------------------------------------


async def test_user_profile_keeps_home_area_and_country(db_session: AsyncSession) -> None:
    user = User(display_name="Test User", home_area="Brooklyn", country="US", language="en")
    db_session.add(user)
    await db_session.flush()

    await db_session.refresh(user)

    assert (user.home_area, user.country, user.language) == ("Brooklyn", "US", "en")


async def test_one_platform_account_cannot_belong_to_two_users(db_session: AsyncSession) -> None:
    first_user = await add_user(db_session)
    second_user = await add_user(db_session)
    db_session.add(
        PlatformAccount(user_id=first_user.id, platform=INSTAGRAM, platform_user_id=SENDER_ID)
    )
    await db_session.flush()

    db_session.add(
        PlatformAccount(user_id=second_user.id, platform=INSTAGRAM, platform_user_id=SENDER_ID)
    )

    with pytest.raises(IntegrityError, match="uq_platform_account_platform_platform_user_id"):
        await db_session.flush()


async def test_unknown_entity_status_is_rejected(db_session: AsyncSession) -> None:
    user = await add_user(db_session)
    restaurant = place("ChIJ-fake-place-1", "Moda Meyhanesi")
    db_session.add(restaurant)
    await db_session.flush()
    db_session.add(UserEntity(user_id=user.id, entity_id=restaurant.id, status="maybe"))

    with pytest.raises(IntegrityError, match="ck_user_entity_status_is_known"):
        await db_session.flush()


async def test_one_item_can_mention_several_entities_but_each_only_once(
    db_session: AsyncSession,
) -> None:
    user = await add_user(db_session)
    item = reel_saved_by(user)
    first_cafe = place("ChIJ-fake-cafe-1", "First café")
    second_cafe = place("ChIJ-fake-cafe-2", "Second café")
    db_session.add_all([item, first_cafe, second_cafe])
    await db_session.flush()
    db_session.add_all(
        [
            ItemEntity(saved_item_id=item.id, entity_id=first_cafe.id, position=0),
            ItemEntity(saved_item_id=item.id, entity_id=second_cafe.id, position=1),
        ]
    )
    await db_session.flush()

    await db_session.execute(
        postgresql.insert(ItemEntity)
        .values(saved_item_id=item.id, entity_id=first_cafe.id)
        .on_conflict_do_nothing()
    )

    assert await count_rows(db_session, ItemEntity, ItemEntity.saved_item_id == item.id) == 2


# --- indexes -----------------------------------------------------------------------


@pytest.mark.parametrize("index_name", sorted(EXPECTED_INDEX_DEFINITIONS))
async def test_search_index_exists_with_the_expected_definition(
    db_session: AsyncSession, index_name: str
) -> None:
    definition = await db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :index_name"),
        {"index_name": index_name},
    )

    assert EXPECTED_INDEX_DEFINITIONS[index_name] in definition.scalar_one()
