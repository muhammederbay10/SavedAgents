"""Hybrid retrieval on a seeded database: filters, ranking, normalization, fallback L1 and L2.

These tests need Postgres with PostGIS and pgvector. See `tests/database.py`.
No model is called: the decision model and the embedder are fakes.
"""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.fallback import FallbackLevel, FallbackReason
from app.agent.filter_normalization import NormalizationMethod
from app.agent.retrieval import AvailabilityGapReason, SearchResult
from app.agent.search_query import CountryRequiredError, Near, SearchQuery
from app.db.models import EntityAvailability
from app.db.types import GeoPoint
from app.models.errors import ModelUnavailableError
from tests.agent.retrieval_support import (
    BEBEK,
    COFFEE_AXIS,
    NOW,
    STUDY_AXIS,
    MappedEmbedder,
    PickingClassifier,
    Saves,
    build_retriever,
    retrieval_settings,
    seed_saves,
)

# A point about 1 km from the seeded café in Bebek, and far from every other place.
NEAR_BEBEK = GeoPoint(latitude=41.0700, longitude=29.0350)
TWO_KILOMETRES = 2_000.0
TWENTY_KILOMETRES = 20_000.0


@pytest.fixture
async def saves(db_session: AsyncSession) -> Saves:
    return await seed_saves(db_session)


def ids(result: SearchResult) -> list[object]:
    return [hit.item_id for hit in result.hits]


def normalized_value(result: SearchResult, slot: str) -> tuple[str, NormalizationMethod]:
    found = next(item for item in result.normalized if item.slot == slot)
    return found.value, found.method


async def test_district_filter_finds_only_places_in_that_district(
    db_session: AsyncSession, saves: Saves
) -> None:
    query = SearchQuery(user_id=saves.user.id, type="place", filters={"area": "Beşiktaş"})

    result = await build_retriever().search(db_session, query)

    assert ids(result) == saves.ids("kronotrop")
    assert result.level is FallbackLevel.NORMAL
    assert result.fallbacks == ()


@pytest.mark.parametrize(
    ("area", "expected"),
    [
        ("Bebek", ["kronotrop"]),
        ("Brooklyn", ["diner"]),
        ("New York", ["diner"]),
        ("Japan", ["izakaya"]),
        ("İstanbul", ["kimchi", "kronotrop"]),
    ],
)
async def test_area_filter_matches_any_address_level_in_any_country(
    db_session: AsyncSession, saves: Saves, area: str, expected: list[str]
) -> None:
    query = SearchQuery(user_id=saves.user.id, type="place", filters={"area": area})

    result = await build_retriever().search(db_session, query)

    assert ids(result) == saves.ids(*expected)


async def test_near_me_keeps_places_within_the_radius_nearest_first(
    db_session: AsyncSession, saves: Saves
) -> None:
    close = SearchQuery(
        user_id=saves.user.id,
        type="place",
        filters={"near": Near(point=NEAR_BEBEK, radius_m=TWO_KILOMETRES)},
    )
    wider = SearchQuery(
        user_id=saves.user.id,
        type="place",
        filters={"near": Near(point=NEAR_BEBEK, radius_m=TWENTY_KILOMETRES)},
    )
    retriever = build_retriever()

    close_result = await retriever.search(db_session, close)
    wider_result = await retriever.search(db_session, wider)

    assert ids(close_result) == saves.ids("kronotrop")
    assert ids(wider_result) == saves.ids("kronotrop", "kimchi")


async def test_genre_and_platform_filter_uses_the_asking_users_country(
    db_session: AsyncSession, saves: Saves
) -> None:
    def horror_on_netflix(country: str) -> SearchQuery:
        return SearchQuery(
            user_id=saves.user.id,
            country=country,
            type="movie",
            filters={"genre": "horror", "streaming": "Netflix"},
        )

    retriever = build_retriever()
    in_us = await retriever.search(db_session, horror_on_netflix("US"))
    in_japan = await retriever.search(db_session, horror_on_netflix("jp"))

    assert ids(in_us) == saves.ids("hereditary")
    assert ids(in_japan) == saves.ids("conjuring")


async def test_a_streaming_filter_needs_the_users_country(
    db_session: AsyncSession, saves: Saves
) -> None:
    query = SearchQuery(user_id=saves.user.id, type="movie", filters={"streaming": "Netflix"})

    with pytest.raises(CountryRequiredError):
        await build_retriever().search(db_session, query)


async def test_another_countrys_row_never_stands_in_for_a_missing_one(
    db_session: AsyncSession, saves: Saves
) -> None:
    # Inception is on Netflix in Japan only. A US user's search must not find it,
    # and must report that the US row is missing.
    query = SearchQuery(
        user_id=saves.user.id,
        country="US",
        type="movie",
        filters={"streaming": "Netflix"},
        collection="Movies",
    )

    result = await build_retriever().search(db_session, query)

    assert ids(result) == saves.ids("hereditary")
    gaps = {(gap.entity_id, gap.country, gap.reason) for gap in result.availability_gaps}
    assert gaps == {
        (saves.entities["inception"].id, "US", AvailabilityGapReason.MISSING),
        (saves.entities["conjuring"].id, "US", AvailabilityGapReason.STALE),
    }


async def test_refreshing_one_country_leaves_the_other_countrys_results_alone(
    db_session: AsyncSession, saves: Saves
) -> None:
    japan_query = SearchQuery(
        user_id=saves.user.id, country="JP", type="movie", filters={"streaming": "Netflix"}
    )
    retriever = build_retriever()
    before = await retriever.search(db_session, japan_query)

    await db_session.execute(
        update(EntityAvailability)
        .where(
            EntityAvailability.entity_id == saves.entities["conjuring"].id,
            EntityAvailability.country == "US",
        )
        .values(providers={"flatrate": ["Netflix"]}, refreshed_at=NOW)
    )
    after = await retriever.search(db_session, japan_query)
    us_after = await retriever.search(
        db_session,
        SearchQuery(
            user_id=saves.user.id, country="US", type="movie", filters={"streaming": "Netflix"}
        ),
    )

    assert ids(after) == ids(before) == saves.ids("conjuring", "inception")
    assert after.availability_gaps == before.availability_gaps
    assert sorted(map(str, ids(us_after))) == sorted(map(str, saves.ids("hereditary", "conjuring")))
    assert saves.entities["conjuring"].id not in {
        gap.entity_id for gap in us_after.availability_gaps
    }


async def test_fuzzy_text_query_ranks_by_meaning(db_session: AsyncSession, saves: Saves) -> None:
    embedder = MappedEmbedder(axes={"somewhere cozy for coffee": COFFEE_AXIS})
    query = SearchQuery(user_id=saves.user.id, text="somewhere cozy for coffee")

    result = await build_retriever(embedder=embedder).search(db_session, query)

    assert ids(result)[0] == saves.items["kronotrop"].id
    assert result.level is FallbackLevel.NORMAL
    assert saves.items["pending"].id not in ids(result)
    assert saves.items["other_place"].id not in ids(result)


async def test_typos_in_filters_are_normalized_to_the_users_own_values(
    db_session: AsyncSession, saves: Saves
) -> None:
    classifier = PickingClassifier(
        picks={("area", "beşilktaş"): "Beşiktaş", ("streaming", "netflx"): "Netflix"}
    )
    retriever = build_retriever(classifier=classifier)

    places = await retriever.search(
        db_session,
        SearchQuery(user_id=saves.user.id, type="place", filters={"area": "beşilktaş"}),
    )
    movies = await retriever.search(
        db_session,
        SearchQuery(
            user_id=saves.user.id, country="US", type="movie", filters={"streaming": "netflx"}
        ),
    )

    assert ids(places) == saves.ids("kronotrop")
    assert normalized_value(places, "filters.area") == ("Beşiktaş", NormalizationMethod.DECISION)
    assert ids(movies) == saves.ids("hereditary")
    assert normalized_value(movies, "filters.streaming") == (
        "Netflix",
        NormalizationMethod.DECISION,
    )


async def test_a_value_the_user_saved_is_an_exact_hit_without_a_decision(
    db_session: AsyncSession, saves: Saves
) -> None:
    classifier = PickingClassifier()
    query = SearchQuery(user_id=saves.user.id, type="place", filters={"area": "  beşiktaş "})

    result = await build_retriever(classifier=classifier).search(db_session, query)

    assert ids(result) == saves.ids("kronotrop")
    assert normalized_value(result, "filters.area") == (
        "Beşiktaş",
        NormalizationMethod.SAVED_VALUE,
    )
    assert classifier.calls == []


async def test_category_and_tags_filter_every_item(db_session: AsyncSession, saves: Saves) -> None:
    retriever = build_retriever()

    cars = await retriever.search(
        db_session, SearchQuery(user_id=saves.user.id, category="car review")
    )
    coffee = await retriever.search(
        db_session, SearchQuery(user_id=saves.user.id, tags=("coffee",))
    )
    both = await retriever.search(
        db_session, SearchQuery(user_id=saves.user.id, category="car review", tags=("bmw",))
    )

    assert ids(cars) == saves.ids("car")
    assert ids(coffee) == saves.ids("kronotrop")
    assert ids(both) == saves.ids("car")


async def test_a_verified_alias_is_an_exact_hit_and_a_candidate_alias_is_not(
    db_session: AsyncSession, saves: Saves
) -> None:
    classifier = PickingClassifier(picks={("category", "restoran"): "restaurant"})
    retriever = build_retriever(classifier=classifier)

    verified = await retriever.search(
        db_session, SearchQuery(user_id=saves.user.id, category="Lokanta")
    )
    candidate = await retriever.search(
        db_session, SearchQuery(user_id=saves.user.id, category="restoran")
    )

    assert normalized_value(verified, "category") == ("restaurant", NormalizationMethod.VOCABULARY)
    assert normalized_value(candidate, "category") == ("restaurant", NormalizationMethod.DECISION)
    assert [wording for wording, _ in classifier.calls] == ["restoran"]
    assert ids(verified) == ids(candidate) == saves.ids("diner", "kimchi")


async def test_a_rejected_alias_is_never_offered_as_a_match(
    db_session: AsyncSession, saves: Saves
) -> None:
    classifier = PickingClassifier(picks={("category", "eatery"): "restaurant"})

    result = await build_retriever(classifier=classifier).search(
        db_session, SearchQuery(user_id=saves.user.id, category="eatery")
    )

    assert "restaurant" not in classifier.offered_values
    assert normalized_value(result, "category") == ("eatery", NormalizationMethod.UNCHANGED)


async def test_normalization_offers_only_the_asking_users_own_values(
    db_session: AsyncSession, saves: Saves
) -> None:
    classifier = PickingClassifier()
    query = SearchQuery(
        user_id=saves.user.id,
        category="grandma recipe",
        type="place",
        filters={"cuisine": "grandma cooking"},
    )

    await build_retriever(classifier=classifier).search(db_session, query)

    offered = classifier.offered_values
    assert offered
    assert "grandma ayşe's recipe" not in offered
    assert "grandma ayşe's cooking" not in offered
    assert offered <= {
        "café",
        "restaurant",
        "bar",
        "horror movie",
        "sci-fi movie",
        "maths tutorial",
        "car review",
        "specialty coffee",
        "korean barbecue",
        "american breakfast",
    }


async def test_a_watched_filter_reads_the_asking_users_status(
    db_session: AsyncSession, saves: Saves
) -> None:
    retriever = build_retriever()

    watched = await retriever.search(
        db_session, SearchQuery(user_id=saves.user.id, type="movie", filters={"watched": True})
    )
    to_watch = await retriever.search(
        db_session, SearchQuery(user_id=saves.user.id, type="movie", filters={"watched": False})
    )

    assert ids(watched) == saves.ids("inception")
    assert ids(to_watch) == saves.ids("hereditary", "conjuring")


async def test_study_filters_read_the_items_fields(db_session: AsyncSession, saves: Saves) -> None:
    query = SearchQuery(
        user_id=saves.user.id,
        type="study",
        filters={"subject": "maths", "topic": "Integrals", "format": "tutorial"},
    )

    result = await build_retriever().search(db_session, query)

    assert ids(result) == saves.ids("integrals")


async def test_failed_embedding_falls_back_to_keyword_search_and_logs_l1(
    db_session: AsyncSession, saves: Saves, caplog: pytest.LogCaptureFixture
) -> None:
    error = ModelUnavailableError("embed timed out", operation="embed", model="fake/embedding")
    query = SearchQuery(user_id=saves.user.id, text="integrals")

    with caplog.at_level(logging.WARNING, logger="app.agent.fallback"):
        result = await build_retriever(embedder=MappedEmbedder(error=error)).search(
            db_session, query
        )

    assert ids(result) == saves.ids("integrals")
    assert result.level is FallbackLevel.DEGRADED
    assert [(step.level, step.reason) for step in result.fallbacks] == [
        (FallbackLevel.DEGRADED, FallbackReason.EMBEDDING_FAILED)
    ]
    logged = [record for record in caplog.records if record.getMessage() == "search_fallback"]
    assert [(record.level, record.reason) for record in logged] == [  # type: ignore[attr-defined]
        ("L1", "embedding_failed")
    ]
    assert "integrals" not in caplog.text


async def test_failed_vector_search_falls_back_to_keyword_search(
    db_session: AsyncSession, saves: Saves
) -> None:
    # A query vector of the wrong size makes the database reject the vector search.
    embedder = MappedEmbedder(axes={"integrals": STUDY_AXIS}, dimension=3)
    query = SearchQuery(user_id=saves.user.id, text="integrals")

    result = await build_retriever(embedder=embedder).search(db_session, query)

    assert ids(result) == saves.ids("integrals")
    assert [(step.level, step.reason) for step in result.fallbacks] == [
        (FallbackLevel.DEGRADED, FallbackReason.VECTOR_SEARCH_FAILED)
    ]


async def test_empty_result_relaxes_filters_and_widens_the_district_to_its_city(
    db_session: AsyncSession, saves: Saves
) -> None:
    query = SearchQuery(
        user_id=saves.user.id,
        type="place",
        filters={"area": "Bebek", "cuisine": "korean barbecue"},
    )

    result = await build_retriever().search(db_session, query)

    assert result.level is FallbackLevel.RELAXED
    assert [(step.level, step.reason) for step in result.fallbacks] == [
        (FallbackLevel.RELAXED, FallbackReason.FEW_RESULTS)
    ]
    assert set(result.relaxed) == {"filters.area", "filters.cuisine"}
    assert ids(result) == saves.ids("kimchi", "kronotrop")
    assert all(hit.level is FallbackLevel.RELAXED for hit in result.hits)


async def test_l2_finds_items_that_are_not_fully_indexed_by_their_caption(
    db_session: AsyncSession, saves: Saves
) -> None:
    query = SearchQuery(
        user_id=saves.user.id,
        collection="Food",
        type="place",
        filters={"area": "Beşiktaş", "cuisine": "lahmacun"},
    )

    result = await build_retriever().search(db_session, query)

    # Beşiktaş is widened to its city, so the café in Beyoğlu is found too.
    assert ids(result) == saves.ids("kimchi", "kronotrop", "pending")
    pending = result.hits[-1]
    assert pending.fully_indexed is False
    assert pending.level is FallbackLevel.RELAXED


async def test_few_results_add_relaxed_ones_after_the_exact_ones(
    db_session: AsyncSession, saves: Saves
) -> None:
    query = SearchQuery(user_id=saves.user.id, type="place", filters={"area": "Bebek"})
    settings = retrieval_settings(search_min_results=3)

    result = await build_retriever(settings=settings).search(db_session, query)

    assert result.hits[0].item_id == saves.items["kronotrop"].id
    assert result.hits[0].level is FallbackLevel.NORMAL
    assert [hit.level for hit in result.hits[1:]] == [FallbackLevel.RELAXED]


async def test_running_out_of_time_returns_an_incomplete_answer(
    db_session: AsyncSession, saves: Saves
) -> None:
    settings = retrieval_settings(search_time_budget_seconds=0.05)
    embedder = MappedEmbedder(delay_seconds=1.0)
    query = SearchQuery(user_id=saves.user.id, text="anything")

    result = await build_retriever(embedder=embedder, settings=settings).search(db_session, query)

    assert result.incomplete is True
    assert result.fallbacks[-1].reason is FallbackReason.TIME_BUDGET_EXCEEDED


async def test_failed_decision_keeps_the_users_wording(
    db_session: AsyncSession, saves: Saves
) -> None:
    error = ModelUnavailableError("decide failed", operation="decide", model="fake")
    classifier = PickingClassifier(picks={("area", "beşilktaş"): "Beşiktaş"}, error=error)
    query = SearchQuery(user_id=saves.user.id, type="place", filters={"area": "beşilktaş"})

    result = await build_retriever(classifier=classifier).search(db_session, query)

    assert normalized_value(result, "filters.area") == ("beşilktaş", NormalizationMethod.UNCHANGED)
    assert result.level is FallbackLevel.RELAXED


async def test_a_low_probability_pick_is_not_used(db_session: AsyncSession, saves: Saves) -> None:
    classifier = PickingClassifier(picks={("area", "beşilktaş"): "Beşiktaş"}, probability=0.4)
    query = SearchQuery(user_id=saves.user.id, type="place", filters={"area": "beşilktaş"})

    result = await build_retriever(classifier=classifier).search(db_session, query)

    assert normalized_value(result, "filters.area") == ("beşilktaş", NormalizationMethod.UNCHANGED)


async def test_saved_after_and_limit_narrow_the_answer(
    db_session: AsyncSession, saves: Saves
) -> None:
    query = SearchQuery(
        user_id=saves.user.id, saved_after=NOW - timedelta(days=2, hours=1), limit=1
    )

    result = await build_retriever().search(db_session, query)

    assert ids(result) == saves.ids("car")


async def test_collection_matches_in_any_case_and_near_ranks_nearest_first(
    db_session: AsyncSession, saves: Saves
) -> None:
    query = SearchQuery(
        user_id=saves.user.id,
        type="place",
        filters={"near": Near(point=BEBEK, radius_m=TWENTY_KILOMETRES)},
        collection="food",
    )

    result = await build_retriever().search(db_session, query)

    assert ids(result) == saves.ids("kronotrop", "kimchi")
