"""Extraction schemas accept open values in any language and reject malformed output."""

import json

import pytest
from pydantic import ValidationError

from app.types.base import EntityExtraction, Extraction, vocabulary_fields
from app.types.movie import MovieExtraction, MovieMention
from app.types.place import PlaceExtraction, PlaceMention
from app.types.registry import CONTENT_TYPES
from app.types.study import StudyExtraction

# A payload is what a text model sent back. It can hold anything, including the
# malformed values these tests feed in on purpose, so its values are only `object`.
Payload = dict[str, object]

PLACE_SINGLE: Payload = {
    "summary": "A third-wave coffee shop in Bebek with a sea view.",
    "category": "coffee shop",
    "tags": ["sea view", "specialty coffee"],
    "entities": [{"name": "Kronotrop", "area_hint": "Bebek", "cuisine": None}],
}
PLACE_LIST: Payload = {
    "summary": "Three places to eat in Brooklyn.",
    "category": "restaurant",
    "tags": [],
    "entities": [
        {"name": "Lucali", "area_hint": "Carroll Gardens, Brooklyn", "cuisine": "pizza"},
        {"name": "Win Son", "area_hint": "Williamsburg", "cuisine": "taiwanese-american"},
        {"name": "Peter Luger", "area_hint": None, "cuisine": None},
    ],
}
PLACE_WITHOUT_ENTITY: Payload = {
    "summary": "A silent reel of a rooftop restaurant. The text names no venue.",
    "category": "rooftop restaurant",
    "tags": ["rooftop"],
    "entities": [],
}

MOVIE_SINGLE: Payload = {
    "summary": "Why Inception's ending still divides people.",
    "category": "movie analysis",
    "tags": ["ending explained"],
    "entities": [{"title": "Inception", "year_hint": 2010}],
}
MOVIE_LIST: Payload = {
    "summary": "Three titles to watch this weekend.",
    "category": "watchlist",
    "tags": ["weekend"],
    "entities": [
        {"title": "Dune: Part Two", "year_hint": 2024},
        {"title": "Kış Uykusu", "year_hint": None},
        {"title": "千と千尋の神隠し", "year_hint": 2001},
    ],
}

STUDY_TUTORIAL: Payload = {
    "summary": "A worked example of integration by parts.",
    "category": "maths tutorial",
    "tags": ["calculus"],
    "subject": "maths",
    "topic": "integrals",
    "format": "tutorial",
    "key_points": ["Pick u so that its derivative gets simpler.", "Check by differentiating."],
    "resources": ["3Blue1Brown"],
    "exam": "A-levels",
}
STUDY_TIP: Payload = {
    "summary": "How to plan reviews with spaced repetition.",
    "category": "study tip",
    "tags": [],
    "subject": "study skills",
    "topic": "spaced repetition",
    "format": "tip",
    "key_points": [],
    "resources": [],
    "exam": None,
}

NO_TYPE_CAR_REVIEW: Payload = {
    "summary": "A track test of the new BMW M3.",
    "category": "car review",
    "tags": ["bmw", "track test"],
}
NO_TYPE_UNTAGGED: Payload = {
    "summary": "Una rutina de cuidado de la piel para la noche.",
    "category": "rutina de skincare",
    "tags": [],
}


def changed(payload: Payload, **changes: object) -> Payload:
    """Return a copy of `payload` with some top-level keys replaced."""
    return {**payload, **changes}


def without(payload: Payload, key: str) -> Payload:
    """Return a copy of `payload` that lacks one top-level key."""
    return {name: value for name, value in payload.items() if name != key}


def place_mention(
    name: object = "Kronotrop", area_hint: object = None, cuisine: object = None
) -> Payload:
    return {"name": name, "area_hint": area_hint, "cuisine": cuisine}


@pytest.mark.parametrize(
    ("payload", "names"),
    [
        (PLACE_SINGLE, ["Kronotrop"]),
        (PLACE_LIST, ["Lucali", "Win Son", "Peter Luger"]),
        (PLACE_WITHOUT_ENTITY, []),
    ],
)
def test_place_schema_accepts_good_examples(payload: Payload, names: list[str]) -> None:
    extraction = PlaceExtraction.model_validate(payload)

    assert [place.name for place in extraction.entities] == names


def test_place_schema_keeps_list_posts_in_order_with_their_fields() -> None:
    extraction = PlaceExtraction.model_validate(PLACE_LIST)

    assert extraction.entities[0].area_hint == "Carroll Gardens, Brooklyn"
    assert extraction.entities[1].cuisine == "taiwanese-american"
    assert extraction.entities[2].area_hint is None
    assert extraction.entities[2].cuisine is None


@pytest.mark.parametrize(
    "area_hint",
    [
        "Beşiktaş",
        "Kadıköy Moda, İstanbul",
        "Brooklyn, New York",
        "渋谷",
        "Shibuya, Tokyo, Japan",
        "حي الزمالك، القاهرة",
        "Condesa, Ciudad de México",
        "서울 성수동",
        "Prenzlauer Berg",
        "Japan",
    ],
)
def test_place_is_accepted_anywhere_at_any_address_level(area_hint: str) -> None:
    payload = changed(PLACE_SINGLE, entities=[place_mention(area_hint=area_hint)])

    extraction = PlaceExtraction.model_validate(payload)

    assert extraction.entities[0].area_hint == area_hint


@pytest.mark.parametrize(
    "cuisine",
    [
        "martian fusion",
        "Kore mutfağı",
        "ラーメン",
        "comida oaxaqueña",
        "مأكولات بحرية",
        "third-wave coffee",
    ],
)
def test_place_accepts_a_cuisine_nobody_listed_in_any_language(cuisine: str) -> None:
    payload = changed(PLACE_SINGLE, entities=[place_mention(cuisine=cuisine)])

    extraction = PlaceExtraction.model_validate(payload)

    assert extraction.entities[0].cuisine == cuisine


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(PLACE_SINGLE, "summary"), id="summary-missing"),
        pytest.param(changed(PLACE_SINGLE, summary="   "), id="summary-blank"),
        pytest.param(without(PLACE_SINGLE, "category"), id="category-missing"),
        pytest.param(without(PLACE_SINGLE, "tags"), id="tags-missing"),
        pytest.param(without(PLACE_SINGLE, "entities"), id="entities-missing"),
        pytest.param(changed(PLACE_SINGLE, entities=["Kronotrop"]), id="entity-is-not-an-object"),
        pytest.param(changed(PLACE_SINGLE, entities=[place_mention(name="")]), id="name-empty"),
        pytest.param(
            changed(PLACE_SINGLE, entities=[{"name": "Kronotrop", "cuisine": None}]),
            id="area-hint-missing",
        ),
        pytest.param(
            changed(PLACE_SINGLE, entities=[place_mention(area_hint="  ")]), id="area-hint-blank"
        ),
        pytest.param(
            changed(PLACE_SINGLE, entities=[place_mention(cuisine="")]), id="cuisine-empty"
        ),
        pytest.param(
            changed(PLACE_SINGLE, entities=[place_mention(cuisine=["pizza", "pasta"])]),
            id="cuisine-is-a-list",
        ),
        pytest.param(
            changed(PLACE_SINGLE, entities=[{**place_mention(), "district": "Beşiktaş"}]),
            id="unknown-key-in-entity",
        ),
        pytest.param(changed(PLACE_SINGLE, area="Beşiktaş"), id="unknown-key"),
    ],
)
def test_place_schema_rejects_malformed_output(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        PlaceExtraction.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "titles"),
    [
        (MOVIE_SINGLE, [("Inception", 2010)]),
        (
            MOVIE_LIST,
            [("Dune: Part Two", 2024), ("Kış Uykusu", None), ("千と千尋の神隠し", 2001)],
        ),
    ],
)
def test_movie_schema_accepts_good_examples(
    payload: Payload, titles: list[tuple[str, int | None]]
) -> None:
    extraction = MovieExtraction.model_validate(payload)

    assert [(movie.title, movie.year_hint) for movie in extraction.entities] == titles


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(MOVIE_SINGLE, "summary"), id="summary-missing"),
        pytest.param(without(MOVIE_SINGLE, "category"), id="category-missing"),
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
            id="unknown-key-in-entity",
        ),
    ],
)
def test_movie_schema_rejects_malformed_output(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        MovieExtraction.model_validate(payload)


def test_study_schema_accepts_a_tutorial_for_an_exam() -> None:
    extraction = StudyExtraction.model_validate(STUDY_TUTORIAL)

    assert extraction.subject == "maths"
    assert extraction.topic == "integrals"
    assert extraction.format == "tutorial"
    assert extraction.exam == "A-levels"
    assert extraction.resources == ("3Blue1Brown",)
    assert len(extraction.key_points) == 2


def test_study_schema_accepts_a_tip_without_exam_points_or_resources() -> None:
    extraction = StudyExtraction.model_validate(STUDY_TIP)

    assert extraction.subject == "study skills"
    assert extraction.exam is None
    assert extraction.key_points == ()
    assert extraction.resources == ()


@pytest.mark.parametrize(
    ("subject", "study_format", "exam"),
    [
        ("astrophysics", "podcast episode", "SAT"),
        ("matematik", "konu anlatımı", "YKS"),
        ("droit constitutionnel", "fiche de révision", "baccalauréat"),
        ("数学", "解説動画", "共通テスト"),
        ("química orgánica", "resumen", "selectividad"),
        ("music theory", "flashcards", "ABRSM Grade 5"),
    ],
)
def test_study_accepts_subjects_formats_and_exams_nobody_listed(
    subject: str, study_format: str, exam: str
) -> None:
    payload = changed(STUDY_TUTORIAL, subject=subject, format=study_format, exam=exam)

    extraction = StudyExtraction.model_validate(payload)

    assert (extraction.subject, extraction.format, extraction.exam) == (
        subject,
        study_format,
        exam,
    )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(STUDY_TUTORIAL, "summary"), id="summary-missing"),
        pytest.param(without(STUDY_TUTORIAL, "category"), id="category-missing"),
        pytest.param(without(STUDY_TUTORIAL, "subject"), id="subject-missing"),
        pytest.param(changed(STUDY_TUTORIAL, subject=" "), id="subject-blank"),
        pytest.param(changed(STUDY_TUTORIAL, subject=None), id="subject-null"),
        pytest.param(changed(STUDY_TUTORIAL, topic=""), id="topic-empty"),
        pytest.param(without(STUDY_TUTORIAL, "format"), id="format-missing"),
        pytest.param(changed(STUDY_TUTORIAL, format=["tip", "tutorial"]), id="format-is-a-list"),
        pytest.param(without(STUDY_TUTORIAL, "key_points"), id="key-points-missing"),
        pytest.param(changed(STUDY_TUTORIAL, key_points=["  "]), id="key-point-blank"),
        pytest.param(changed(STUDY_TUTORIAL, resources="3Blue1Brown"), id="resources-not-a-list"),
        pytest.param(changed(STUDY_TUTORIAL, exam=""), id="exam-empty"),
        pytest.param(without(STUDY_TUTORIAL, "exam"), id="exam-missing"),
        pytest.param(changed(STUDY_TUTORIAL, level="beginner"), id="unknown-key"),
    ],
)
def test_study_schema_rejects_malformed_output(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        StudyExtraction.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "category", "tags"),
    [
        (NO_TYPE_CAR_REVIEW, "car review", ("bmw", "track test")),
        (NO_TYPE_UNTAGGED, "rutina de skincare", ()),
    ],
)
def test_post_without_a_built_in_type_keeps_its_category_tags_and_summary(
    payload: Payload, category: str, tags: tuple[str, ...]
) -> None:
    extraction = CONTENT_TYPES.extraction_schema(None).model_validate(payload)

    assert extraction.category == category
    assert extraction.tags == tags
    assert extraction.summary == payload["summary"]


@pytest.mark.parametrize(
    ("category", "tags"),
    [
        ("skincare routine", ["retinol", "night routine"]),
        ("araba incelemesi", ["elektrikli", "uzun yol"]),
        ("ギターレッスン", ["初心者", "コード"]),
        ("receta vegana", ["sin gluten"]),
        ("وصفة حلويات", ["رمضان"]),
        ("крафтовое пиво", ["дегустация"]),
        ("DIY woodworking", []),
    ],
)
def test_any_category_and_tags_are_accepted_in_any_language(category: str, tags: list[str]) -> None:
    payload = changed(NO_TYPE_CAR_REVIEW, category=category, tags=tags)

    extraction = Extraction.model_validate(payload)

    assert extraction.category == category
    assert list(extraction.tags) == tags


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(without(NO_TYPE_CAR_REVIEW, "summary"), id="summary-missing"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, summary=""), id="summary-empty"),
        pytest.param(without(NO_TYPE_CAR_REVIEW, "category"), id="category-missing"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, category="  "), id="category-blank"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, category=None), id="category-null"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, category=["car", "review"]), id="category-list"),
        pytest.param(without(NO_TYPE_CAR_REVIEW, "tags"), id="tags-missing"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, tags=["bmw", " "]), id="tag-blank"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, tags="bmw"), id="tags-not-a-list"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, tags=[{"name": "bmw"}]), id="tag-is-an-object"),
        pytest.param(changed(NO_TYPE_CAR_REVIEW, entities=[]), id="unknown-key"),
    ],
)
def test_post_without_a_built_in_type_rejects_malformed_output(payload: Payload) -> None:
    with pytest.raises(ValidationError):
        CONTENT_TYPES.extraction_schema(None).model_validate(payload)


def test_text_is_stored_without_surrounding_whitespace() -> None:
    payload = changed(
        PLACE_SINGLE,
        summary="  A coffee shop.  ",
        category=" coffee shop\n",
        tags=[" sea view "],
        entities=[place_mention(name=" Kronotrop\n", area_hint=" Bebek ", cuisine=" coffee ")],
    )

    extraction = PlaceExtraction.model_validate(payload)

    assert extraction.summary == "A coffee shop."
    assert extraction.category == "coffee shop"
    assert extraction.tags == ("sea view",)
    assert extraction.entities[0].name == "Kronotrop"
    assert extraction.entities[0].area_hint == "Bebek"
    assert extraction.entities[0].cuisine == "coffee"


def test_schema_parses_the_json_text_a_model_returns() -> None:
    extraction = MovieExtraction.model_validate_json(json.dumps(MOVIE_LIST))

    assert [movie.title for movie in extraction.entities] == [
        "Dune: Part Two",
        "Kış Uykusu",
        "千と千尋の神隠し",
    ]


def test_an_extraction_cannot_be_changed_after_validation() -> None:
    extraction = Extraction.model_validate(NO_TYPE_CAR_REVIEW)

    with pytest.raises(ValidationError):
        extraction.summary = "Something else."  # type: ignore[misc]


GOOD_EXAMPLE_BY_TYPE: dict[str, Payload] = {
    "place": PLACE_LIST,
    "movie": MOVIE_LIST,
    "study": STUDY_TUTORIAL,
}


def test_every_registered_type_has_a_good_example_here() -> None:
    registered = {content_type.name for content_type in CONTENT_TYPES.all()}

    assert set(GOOD_EXAMPLE_BY_TYPE) == registered


@pytest.mark.parametrize("type_name", sorted(GOOD_EXAMPLE_BY_TYPE))
def test_registered_schema_validates_its_good_example(type_name: str) -> None:
    example = GOOD_EXAMPLE_BY_TYPE[type_name]

    extraction = CONTENT_TYPES.extraction_schema(type_name).model_validate(example)

    assert extraction.summary == example["summary"]
    assert extraction.category == example["category"]


@pytest.mark.parametrize("type_name", sorted(GOOD_EXAMPLE_BY_TYPE))
def test_registered_schema_rejects_output_without_its_own_fields(type_name: str) -> None:
    schema = CONTENT_TYPES.extraction_schema(type_name)

    with pytest.raises(ValidationError):
        schema.model_validate(NO_TYPE_CAR_REVIEW)


@pytest.mark.parametrize("type_name", [None, *sorted(GOOD_EXAMPLE_BY_TYPE)])
def test_every_schema_exports_a_strict_json_schema_with_the_open_fields(
    type_name: str | None,
) -> None:
    json_schema = CONTENT_TYPES.extraction_schema(type_name).model_json_schema()

    assert json_schema["additionalProperties"] is False
    assert {"summary", "category", "tags"} <= set(json_schema["required"])


@pytest.mark.parametrize("type_name", [None, *sorted(GOOD_EXAMPLE_BY_TYPE)])
def test_no_schema_restricts_a_value_to_a_closed_list(type_name: str | None) -> None:
    json_schema = CONTENT_TYPES.extraction_schema(type_name).model_json_schema()

    assert '"enum"' not in json.dumps(json_schema)
    assert '"const"' not in json.dumps(json_schema)


def test_entity_types_expose_their_mentions_through_the_shared_base() -> None:
    extraction = CONTENT_TYPES.extraction_schema("movie").model_validate(MOVIE_LIST)

    assert isinstance(extraction, EntityExtraction)
    assert len(extraction.entities) == 3


@pytest.mark.parametrize("type_name", [None, *sorted(GOOD_EXAMPLE_BY_TYPE)])
def test_every_schema_marks_category_and_tags_for_the_vocabulary(type_name: str | None) -> None:
    marked = vocabulary_fields(CONTENT_TYPES.extraction_schema(type_name))

    assert marked["category"] == "category"
    assert marked["tags"] == "tag"


def test_place_marks_cuisine_for_the_vocabulary_and_leaves_names_and_areas_alone() -> None:
    assert vocabulary_fields(PlaceMention) == {"cuisine": "cuisine"}


def test_movie_mention_has_no_value_to_normalize() -> None:
    assert vocabulary_fields(MovieMention) == {}


def test_study_marks_subject_format_and_exam_for_the_vocabulary() -> None:
    assert vocabulary_fields(StudyExtraction) == {
        "category": "category",
        "tags": "tag",
        "subject": "subject",
        "format": "format",
        "exam": "exam",
    }
