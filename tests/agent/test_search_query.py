import uuid
from typing import Any

import pytest

from app.agent.search_conditions import MENTIONS_KEY
from app.agent.search_query import (
    CountryRequiredError,
    Near,
    SearchPlan,
    SearchQuery,
    SearchQueryError,
    UnsupportedFilterError,
    plan_search,
)
from app.db.types import GeoPoint
from app.types.base import EntityExtraction
from app.types.registry import CONTENT_TYPES

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SOMEWHERE = GeoPoint(latitude=40.0, longitude=-3.7)


def plan(**query: Any) -> SearchPlan:  # noqa: ANN401 - each test passes its own query fields
    return plan_search(SearchQuery(user_id=USER_ID, **query), CONTENT_TYPES)


def test_wordings_are_listed_for_every_value_the_user_writes() -> None:
    planned = plan(
        collection="Food",
        category="restaurant",
        tags=("rooftop", "cheap"),
        type="place",
        filters={"area": "Shibuya", "cuisine": "ramen", "near": Near(SOMEWHERE, 500.0)},
    )

    assert [wording.slot for wording in planned.wordings] == [
        "collection",
        "category",
        "tags.0",
        "tags.1",
        "filters.area",
        "filters.cuisine",
    ]
    assert [wording.vocabulary for wording in planned.wordings] == [
        None,
        "category",
        "tag",
        "tag",
        None,
        "cuisine",
    ]


def test_an_unknown_type_names_the_known_ones() -> None:
    with pytest.raises(
        SearchQueryError, match=r"Unknown content type 'recipe'\. Known types: place"
    ):
        plan(type="recipe")


def test_filters_need_a_type() -> None:
    with pytest.raises(SearchQueryError, match="give the type as well"):
        plan(filters={"area": "Brooklyn"})


def test_a_filter_the_type_does_not_have_is_rejected() -> None:
    with pytest.raises(SearchQueryError, match="'genre' is not a filter of the type 'place'"):
        plan(type="place", filters={"genre": "horror"})


@pytest.mark.parametrize(
    ("type_name", "filters"),
    [
        ("place", {"near": "Brooklyn"}),
        ("movie", {"year": "2010"}),
        ("movie", {"year": True}),
        ("movie", {"watched": "yes"}),
        ("place", {"area": 34}),
    ],
)
def test_a_value_of_the_wrong_kind_is_rejected(type_name: str, filters: dict[str, object]) -> None:
    with pytest.raises(SearchQueryError, match="takes a"):
        plan(type=type_name, filters=filters)


@pytest.mark.parametrize(
    "query",
    [
        {"category": "  "},
        {"collection": ""},
        {"tags": ("ok", " ")},
        {"type": "place", "filters": {"area": " "}},
    ],
)
def test_a_blank_value_is_rejected(query: dict[str, object]) -> None:
    with pytest.raises(SearchQueryError, match="blank"):
        plan(**query)


def test_a_streaming_filter_without_a_country_asks_for_it() -> None:
    with pytest.raises(CountryRequiredError, match="Ask the user for it once"):
        plan(type="movie", filters={"streaming": "Netflix"}, country=" ")


def test_open_now_is_not_supported_until_opening_hours_are_stored() -> None:
    with pytest.raises(UnsupportedFilterError, match="open_now"):
        plan(type="place", filters={"open_now": True})


def test_a_radius_must_be_positive() -> None:
    with pytest.raises(SearchQueryError, match="radius above 0"):
        Near(SOMEWHERE, 0.0)


def test_a_limit_must_be_positive() -> None:
    with pytest.raises(SearchQueryError, match="at least 1"):
        plan(limit=0)


def test_only_top_level_schema_fields_describe_the_whole_item() -> None:
    study = plan(type="study", filters={"subject": "maths", "topic": "integrals"})
    place = plan(type="place", filters={"cuisine": "ramen", "price": "cheap"})
    movie = plan(type="movie", filters={"genre": "horror", "year": 2018})

    assert [typed.item_wide for typed in study.filters] == [True, True]
    assert [typed.item_wide for typed in place.filters] == [False, False]
    assert [typed.item_wide for typed in movie.filters] == [False, False]


def test_mentions_are_read_from_the_entity_extractions_own_field() -> None:
    assert MENTIONS_KEY in EntityExtraction.model_fields
