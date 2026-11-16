"""Each content type's extraction schema accepts good text-model output and rejects bad output."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.types.base import EntityExtraction
from app.types.movie import MovieExtraction
from app.types.other import OtherExtraction
from app.types.place import Cuisine, PlaceExtraction
from app.types.registry import CONTENT_TYPES
from app.types.study import Exam, StudyExtraction, StudySubject

Payload = dict[str, Any]

PLACE_SINGLE: Payload = {
    "summary": "A third-wave coffee shop in Bebek with a sea view.",
    "entities": [{"name": "Kronotrop", "area_hint": "Bebek", "cuisine": None}],
}
PLACE_LIST: Payload = {
    "summary": "Kadıköy'de gidilecek 3 mekan.",
    "entities": [
        {"name": "Çiya Sofrası", "area_hint": "Kadıköy", "cuisine": "turkish"},
        {"name": "Basta Street Food Bar", "area_hint": "Kadıköy Moda", "cuisine": "street food"},
        {"name": "Walter's Coffee Roastery", "area_hint": None, "cuisine": None},
    ],
}
PLACE_WITHOUT_ENTITY: Payload = {
    "summary": "A silent reel of a rooftop restaurant. The text names no venue.",
    "entities": [],
}

MOVIE_SINGLE: Payload = {
    "summary": "Why Inception's ending still divides people.",
    "entities": [{"title": "Inception", "year_hint": 2010}],
}
MOVIE_LIST: Payload = {
    "summary": "Three films to watch this weekend.",
    "entities": [
        {"title": "Dune: Part Two", "year_hint": 2024},
        {"title": "Kış Uykusu", "year_hint": None},
        {"title": "Severance", "year_hint": None},
    ],
}

STUDY_TUTORIAL: Payload = {
    "summary": "A worked example of integration by parts.",
    "subject": "maths",
    "topic": "integrals",
    "key_points": ["Pick u so that its derivative gets simpler.", "Check by differentiating."],
    "resources": ["3Blue1Brown"],
    "exam": "YKS",
}
STUDY_TIP: Payload = {
    "summary": "How to plan reviews with spaced repetition.",
    "subject": "study skills",
    "topic": "spaced repetition",
    "key_points": [],
    "resources": [],
    "exam": None,
}

OTHER_TAGGED: Payload = {
    "summary": "A 20-minute home workout without equipment.",
    "tags": ["home workout", "fitness"],
}
OTHER_UNTAGGED: Payload = {"summary": "A sunset over the Bosphorus.", "tags": []}


def changed(payload: Payload, **changes: object) -> Payload:
    """Return a copy of `payload` with some top-level keys replaced."""
    return {**payload, **changes}


def without(payload: Payload, key: str) -> Payload:
    """Return a copy of `payload` that lacks one top-level key."""
    return {name: value for name, value in payload.items() if name != key}


@pytest.mark.parametrize("payload", [PLACE_SINGLE, PLACE_LIST, PLACE_WITHOUT_ENTITY])
def test_place_schema_accepts_good_examples(payload: Payload) -> None:
    extraction = PlaceExtraction.model_validate(payload)

    assert [place.name for place in extraction.entities] == [
        entity["name"] for entity in payload["entities"]
    ]


def test_place_schema_keeps_list_posts_in_order_with_their_fields() -> None:
    extraction = PlaceExtraction.model_validate(PLACE_LIST)

    assert extraction.entities[1].area_hint == "Kadıköy Moda"
    assert extraction.entities[1].cuisine is Cuisine.STREET_FOOD
    assert extraction.entities[2].area_hint is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(PLACE_SINGLE, "summary"), id="summary-missing"),
        pytest.param(changed(PLACE_SINGLE, summary="   "), id="summary-blank"),
        pytest.param(without(PLACE_SINGLE, "entities"), id="entities-missing"),
        pytest.param(changed(PLACE_SINGLE, entities=["Kronotrop"]), id="entity-is-not-an-object"),
        pytest.param(
            changed(PLACE_SINGLE, entities=[{"name": "", "area_hint": None, "cuisine": None}]),
            id="name-empty",
        ),
        pytest.param(
            changed(PLACE_SINGLE, entities=[{"name": "Kronotrop", "cuisine": None}]),
            id="area-hint-missing",
        ),
        pytest.param(
            changed(
                PLACE_SINGLE,
                entities=[{"name": "Kronotrop", "area_hint": None, "cuisine": "martian"}],
            ),
            id="cuisine-unknown",
        ),
        pytest.param(changed(PLACE_SINGLE, district="Beşiktaş"), id="unknown-key"),
    ],
)
def test_place_schema_rejects_bad_examples(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        PlaceExtraction.model_validate(payload)


@pytest.mark.parametrize("payload", [MOVIE_SINGLE, MOVIE_LIST])
def test_movie_schema_accepts_good_examples(payload: Payload) -> None:
    extraction = MovieExtraction.model_validate(payload)

    assert [(movie.title, movie.year_hint) for movie in extraction.entities] == [
        (entity["title"], entity["year_hint"]) for entity in payload["entities"]
    ]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(MOVIE_SINGLE, "summary"), id="summary-missing"),
        pytest.param(without(MOVIE_SINGLE, "entities"), id="entities-missing"),
        pytest.param(
            changed(MOVIE_SINGLE, entities=[{"title": " ", "year_hint": 2010}]),
            id="title-blank",
        ),
        pytest.param(changed(MOVIE_SINGLE, entities=[{"title": "Inception"}]), id="year-missing"),
        pytest.param(
            changed(MOVIE_SINGLE, entities=[{"title": "Inception", "year_hint": 148}]),
            id="year-is-a-runtime",
        ),
        pytest.param(
            changed(MOVIE_SINGLE, entities=[{"title": "Inception", "year_hint": 20100}]),
            id="year-too-late",
        ),
        pytest.param(
            changed(MOVIE_SINGLE, entities=[{"title": "Inception", "year_hint": "recent"}]),
            id="year-is-text",
        ),
        pytest.param(
            changed(
                MOVIE_SINGLE,
                entities=[{"title": "Inception", "year_hint": 2010, "genre": "science fiction"}],
            ),
            id="unknown-key",
        ),
    ],
)
def test_movie_schema_rejects_bad_examples(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        MovieExtraction.model_validate(payload)


def test_study_schema_accepts_a_tutorial_for_an_exam() -> None:
    extraction = StudyExtraction.model_validate(STUDY_TUTORIAL)

    assert extraction.subject is StudySubject.MATHS
    assert extraction.topic == "integrals"
    assert extraction.exam is Exam.YKS
    assert extraction.resources == ("3Blue1Brown",)
    assert len(extraction.key_points) == 2


def test_study_schema_accepts_a_tip_without_exam_points_or_resources() -> None:
    extraction = StudyExtraction.model_validate(STUDY_TIP)

    assert extraction.subject is StudySubject.STUDY_SKILLS
    assert extraction.exam is None
    assert extraction.key_points == ()
    assert extraction.resources == ()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(STUDY_TUTORIAL, "summary"), id="summary-missing"),
        pytest.param(without(STUDY_TUTORIAL, "subject"), id="subject-missing"),
        pytest.param(changed(STUDY_TUTORIAL, subject="astrology"), id="subject-unknown"),
        pytest.param(changed(STUDY_TUTORIAL, topic=""), id="topic-empty"),
        pytest.param(without(STUDY_TUTORIAL, "key_points"), id="key-points-missing"),
        pytest.param(changed(STUDY_TUTORIAL, key_points=["  "]), id="key-point-blank"),
        pytest.param(changed(STUDY_TUTORIAL, resources="3Blue1Brown"), id="resources-not-a-list"),
        pytest.param(changed(STUDY_TUTORIAL, exam="SAT"), id="exam-unknown"),
        pytest.param(without(STUDY_TUTORIAL, "exam"), id="exam-missing"),
        pytest.param(changed(STUDY_TUTORIAL, format="tutorial"), id="unknown-key"),
    ],
)
def test_study_schema_rejects_bad_examples(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        StudyExtraction.model_validate(payload)


@pytest.mark.parametrize("payload", [OTHER_TAGGED, OTHER_UNTAGGED])
def test_other_schema_accepts_good_examples(payload: Payload) -> None:
    extraction = OtherExtraction.model_validate(payload)

    assert list(extraction.tags) == payload["tags"]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(OTHER_TAGGED, "summary"), id="summary-missing"),
        pytest.param(changed(OTHER_TAGGED, summary=""), id="summary-empty"),
        pytest.param(without(OTHER_TAGGED, "tags"), id="tags-missing"),
        pytest.param(changed(OTHER_TAGGED, tags=["fitness", " "]), id="tag-blank"),
        pytest.param(changed(OTHER_TAGGED, tags="fitness"), id="tags-not-a-list"),
        pytest.param(changed(OTHER_TAGGED, entities=[]), id="unknown-key"),
    ],
)
def test_other_schema_rejects_bad_examples(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        OtherExtraction.model_validate(payload)


def test_text_is_stored_without_surrounding_whitespace() -> None:
    payload = changed(
        PLACE_SINGLE,
        summary="  A coffee shop.  ",
        entities=[{"name": " Kronotrop\n", "area_hint": " Bebek ", "cuisine": None}],
    )

    extraction = PlaceExtraction.model_validate(payload)

    assert extraction.summary == "A coffee shop."
    assert extraction.entities[0].name == "Kronotrop"
    assert extraction.entities[0].area_hint == "Bebek"


def test_schema_parses_the_json_text_a_model_returns() -> None:
    extraction = MovieExtraction.model_validate_json(json.dumps(MOVIE_LIST))

    assert [movie.title for movie in extraction.entities] == [
        "Dune: Part Two",
        "Kış Uykusu",
        "Severance",
    ]


def test_an_extraction_cannot_be_changed_after_validation() -> None:
    extraction = OtherExtraction.model_validate(OTHER_TAGGED)

    with pytest.raises(ValidationError):
        extraction.summary = "Something else."  # type: ignore[misc]


GOOD_EXAMPLE_BY_TYPE: dict[str, Payload] = {
    "place": PLACE_LIST,
    "movie": MOVIE_LIST,
    "study": STUDY_TUTORIAL,
    "other": OTHER_TAGGED,
}


def test_every_registered_type_has_a_good_example_here() -> None:
    registered = {content_type.name for content_type in CONTENT_TYPES.all()}

    assert set(GOOD_EXAMPLE_BY_TYPE) == registered


@pytest.mark.parametrize("type_name", sorted(GOOD_EXAMPLE_BY_TYPE))
def test_registered_schema_validates_its_good_example(type_name: str) -> None:
    schema = CONTENT_TYPES.get(type_name).extraction_schema

    extraction = schema.model_validate(GOOD_EXAMPLE_BY_TYPE[type_name])

    assert extraction.summary == GOOD_EXAMPLE_BY_TYPE[type_name]["summary"]


@pytest.mark.parametrize("type_name", sorted(GOOD_EXAMPLE_BY_TYPE))
def test_registered_schema_rejects_another_types_output(type_name: str) -> None:
    schema = CONTENT_TYPES.get(type_name).extraction_schema
    foreign_payloads = [
        payload for name, payload in GOOD_EXAMPLE_BY_TYPE.items() if name != type_name
    ]

    for payload in foreign_payloads:
        with pytest.raises(ValidationError):
            schema.model_validate(payload)


@pytest.mark.parametrize("type_name", sorted(GOOD_EXAMPLE_BY_TYPE))
def test_registered_schema_exports_a_strict_json_schema(type_name: str) -> None:
    json_schema = CONTENT_TYPES.get(type_name).extraction_schema.model_json_schema()

    assert json_schema["additionalProperties"] is False
    assert "summary" in json_schema["required"]


def test_entity_types_expose_their_mentions_through_the_shared_base() -> None:
    extraction = CONTENT_TYPES.get("movie").extraction_schema.model_validate(MOVIE_LIST)

    assert isinstance(extraction, EntityExtraction)
    assert len(extraction.entities) == 3
