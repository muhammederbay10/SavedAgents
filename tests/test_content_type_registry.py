"""The registry lists the Phase 1 types, and a type's declaration must be coherent."""

import ast
from pathlib import Path

import pytest

from app.types.base import (
    ContentType,
    ContentTypeDefinitionError,
    EntityExtraction,
    EntityMention,
    Extraction,
    FilterKind,
    SearchFilter,
    SubtypeQuestion,
    TagQuestion,
)
from app.types.registry import CONTENT_TYPES, ContentTypeRegistry, UnknownContentTypeError

PHASE_1_TYPE_NAMES = ["place", "movie", "study", "other"]
ISTANBUL_DISTRICT_COUNT = 39

APP_DIR = Path(__file__).parent.parent / "app"
# The only folders that may hold logic for one specific content type (ARCHITECTURE §6).
CONTENT_TYPE_LOGIC_DIRS = (APP_DIR / "types", APP_DIR / "resolvers")


class RecipeExtraction(Extraction):
    """A schema for a made-up type without entities."""

    dish: str


class BookMention(EntityMention):
    """A mention for a made-up type with entities."""

    title: str


class BookExtraction(EntityExtraction):
    """A schema for a made-up type with entities."""

    entities: tuple[BookMention, ...]


def make_content_type(
    *,
    name: str = "recipe",
    extraction_schema: type[Extraction] = RecipeExtraction,
    tag_questions: tuple[TagQuestion, ...] = (),
    resolver: str | None = None,
    search_filters: tuple[SearchFilter, ...] = (),
) -> ContentType:
    return ContentType(
        name=name,
        description="A made-up type for tests.",
        extraction_schema=extraction_schema,
        subtype_question=None,
        tag_questions=tag_questions,
        resolver=resolver,
        search_filters=search_filters,
    )


def filter_names(type_name: str) -> list[str]:
    return [search_filter.name for search_filter in CONTENT_TYPES.get(type_name).search_filters]


def allowed_values(type_name: str, filter_name: str) -> tuple[str, ...]:
    matches = [
        search_filter
        for search_filter in CONTENT_TYPES.get(type_name).search_filters
        if search_filter.name == filter_name
    ]
    assert len(matches) == 1
    return matches[0].allowed_values


def test_registry_lists_every_phase_1_type() -> None:
    assert [content_type.name for content_type in CONTENT_TYPES.all()] == PHASE_1_TYPE_NAMES


@pytest.mark.parametrize("type_name", PHASE_1_TYPE_NAMES)
def test_registry_returns_a_type_by_its_name(type_name: str) -> None:
    assert CONTENT_TYPES.get(type_name).name == type_name


def test_unknown_type_name_fails_and_names_the_registered_types() -> None:
    with pytest.raises(UnknownContentTypeError, match=r"'podcast'.*place, movie, study, other"):
        CONTENT_TYPES.get("podcast")


def test_a_new_type_is_found_once_it_is_registered() -> None:
    recipe = make_content_type(name="recipe")

    registry = ContentTypeRegistry((*CONTENT_TYPES.all(), recipe))

    assert registry.get("recipe") is recipe
    assert [content_type.name for content_type in registry.all()] == [
        *PHASE_1_TYPE_NAMES,
        "recipe",
    ]


def test_registering_the_same_name_twice_is_rejected() -> None:
    with pytest.raises(ContentTypeDefinitionError, match="'recipe' is registered twice"):
        ContentTypeRegistry((make_content_type(name="recipe"), make_content_type(name="recipe")))


@pytest.mark.parametrize("type_name", PHASE_1_TYPE_NAMES)
def test_every_type_describes_itself_for_the_type_decision(type_name: str) -> None:
    assert CONTENT_TYPES.get(type_name).description.strip()


@pytest.mark.parametrize(
    ("type_name", "subtypes"),
    [
        ("place", ("restaurant", "cafe", "bar", "shop", "attraction")),
        ("movie", ("movie", "series")),
        ("study", ("tip", "tutorial", "explainer", "course")),
    ],
)
def test_type_declares_its_subtype_question(type_name: str, subtypes: tuple[str, ...]) -> None:
    question = CONTENT_TYPES.get(type_name).subtype_question

    assert question is not None
    assert question.text.endswith("?")
    assert question.options == subtypes


@pytest.mark.parametrize("type_name", ["place", "movie", "study"])
def test_type_declares_its_tag_questions(type_name: str) -> None:
    questions = CONTENT_TYPES.get(type_name).tag_questions

    assert questions
    for question in questions:
        assert question.tag == question.tag.strip().lower()
        assert question.text.endswith("?")


def test_other_asks_the_decision_model_nothing() -> None:
    other = CONTENT_TYPES.get("other")

    assert other.subtype_question is None
    assert other.tag_questions == ()


@pytest.mark.parametrize(
    ("type_name", "resolver"),
    [("place", "google_places"), ("movie", "tmdb"), ("study", None), ("other", None)],
)
def test_type_declares_its_resolver(type_name: str, resolver: str | None) -> None:
    assert CONTENT_TYPES.get(type_name).resolver == resolver


@pytest.mark.parametrize(
    ("type_name", "expected_filters"),
    [
        ("place", ["district", "near", "open_now", "cuisine", "price"]),
        ("movie", ["genre", "year", "streaming", "watched"]),
        ("study", ["subject", "topic", "format", "exam"]),
        ("other", ["tags"]),
    ],
)
def test_type_offers_the_filters_from_the_design(
    type_name: str, expected_filters: list[str]
) -> None:
    assert filter_names(type_name) == expected_filters


@pytest.mark.parametrize("type_name", PHASE_1_TYPE_NAMES)
def test_every_filter_is_described_for_the_response_agent(type_name: str) -> None:
    for search_filter in CONTENT_TYPES.get(type_name).search_filters:
        assert search_filter.description.strip()


def test_district_filter_allows_the_39_istanbul_districts() -> None:
    districts = allowed_values("place", "district")

    assert len(districts) == ISTANBUL_DISTRICT_COUNT
    assert {"Beşiktaş", "Kadıköy", "Üsküdar", "Şişli", "Eyüpsultan"} <= set(districts)


@pytest.mark.parametrize(
    ("type_name", "filter_name", "example_values"),
    [
        ("place", "cuisine", {"turkish", "seafood", "other"}),
        ("place", "price", {"inexpensive", "very expensive"}),
        ("movie", "genre", {"horror", "science fiction", "documentary"}),
        ("movie", "streaming", {"Netflix", "Disney+", "MUBI"}),
        ("study", "subject", {"maths", "english", "programming"}),
        ("study", "exam", {"YKS", "YDS", "IELTS"}),
    ],
)
def test_choice_filter_allows_the_values_from_the_design(
    type_name: str, filter_name: str, example_values: set[str]
) -> None:
    assert example_values <= set(allowed_values(type_name, filter_name))


def test_study_format_filter_allows_exactly_the_study_subtypes() -> None:
    question = CONTENT_TYPES.get("study").subtype_question

    assert question is not None
    assert allowed_values("study", "format") == question.options


@pytest.mark.parametrize(
    ("type_name", "filter_name", "kind"),
    [
        ("place", "near", FilterKind.LOCATION),
        ("place", "open_now", FilterKind.BOOLEAN),
        ("movie", "year", FilterKind.INTEGER),
        ("movie", "watched", FilterKind.BOOLEAN),
        ("study", "topic", FilterKind.TEXT),
        ("other", "tags", FilterKind.TEXT),
    ],
)
def test_open_ended_filter_declares_its_kind_and_no_allowed_values(
    type_name: str, filter_name: str, kind: FilterKind
) -> None:
    matches = [
        search_filter
        for search_filter in CONTENT_TYPES.get(type_name).search_filters
        if search_filter.name == filter_name
    ]

    assert [search_filter.kind for search_filter in matches] == [kind]
    assert matches[0].allowed_values == ()


@pytest.mark.parametrize("options", [(), ("only",)])
def test_subtype_question_needs_at_least_two_options(options: tuple[str, ...]) -> None:
    with pytest.raises(ContentTypeDefinitionError, match="at least 2 options"):
        SubtypeQuestion(text="Which one?", options=options)


def test_subtype_question_rejects_a_repeated_option() -> None:
    with pytest.raises(ContentTypeDefinitionError, match="Duplicate subtype options: cafe"):
        SubtypeQuestion(text="Which one?", options=("cafe", "bar", "cafe"))


def test_choice_filter_needs_allowed_values() -> None:
    with pytest.raises(ContentTypeDefinitionError, match="'diet' is a choice"):
        SearchFilter(name="diet", kind=FilterKind.CHOICE, description="The diet.")


@pytest.mark.parametrize(
    "kind", [FilterKind.BOOLEAN, FilterKind.INTEGER, FilterKind.TEXT, FilterKind.LOCATION]
)
def test_open_ended_filter_cannot_have_allowed_values(kind: FilterKind) -> None:
    with pytest.raises(ContentTypeDefinitionError, match="'diet' is not a choice"):
        SearchFilter(name="diet", kind=kind, description="The diet.", allowed_values=("vegan",))


def test_choice_filter_rejects_a_repeated_allowed_value() -> None:
    with pytest.raises(ContentTypeDefinitionError, match="filter 'diet': vegan"):
        SearchFilter(
            name="diet",
            kind=FilterKind.CHOICE,
            description="The diet.",
            allowed_values=("vegan", "halal", "vegan"),
        )


def test_type_rejects_two_questions_for_the_same_tag() -> None:
    questions = (
        TagQuestion(tag="quick", text="Is it quick?"),
        TagQuestion(tag="quick", text="Does it take under 30 minutes?"),
    )

    with pytest.raises(ContentTypeDefinitionError, match="tags in content type 'recipe': quick"):
        make_content_type(tag_questions=questions)


def test_type_rejects_two_filters_with_the_same_name() -> None:
    filters = (
        SearchFilter(name="time", kind=FilterKind.INTEGER, description="Minutes to cook."),
        SearchFilter(name="time", kind=FilterKind.TEXT, description="When to eat it."),
    )

    with pytest.raises(ContentTypeDefinitionError, match="filters in content type 'recipe': time"):
        make_content_type(search_filters=filters)


def test_type_with_a_resolver_must_extract_entities() -> None:
    with pytest.raises(ContentTypeDefinitionError, match="must extend EntityExtraction"):
        make_content_type(extraction_schema=RecipeExtraction, resolver="recipe_site")


def test_type_that_extracts_entities_must_have_a_resolver() -> None:
    with pytest.raises(ContentTypeDefinitionError, match="needs a resolver"):
        make_content_type(name="book", extraction_schema=BookExtraction, resolver=None)


def test_type_with_entities_and_a_resolver_is_accepted() -> None:
    book = make_content_type(name="book", extraction_schema=BookExtraction, resolver="open_library")

    assert book.resolver == "open_library"


def string_constants(node: ast.expr) -> set[str]:
    """Collect the string literals in an expression, looking inside tuples, lists and sets."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return set().union(*(string_constants(element) for element in node.elts))
    return set()


def lines_comparing_against(source: str, names: set[str]) -> list[int]:
    """Find the lines that compare something with one of `names`, or `match` on one."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            compared = string_constants(ast.Tuple(elts=[node.left, *node.comparators]))
            if names & compared:
                lines.append(node.lineno)
        elif isinstance(node, ast.MatchValue) and names & string_constants(node.value):
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize(
    "source",
    [
        'if item_type == "movie":\n    pass\n',
        'if "place" != item_type:\n    pass\n',
        'needs_entity = item_type in ("place", "movie")\n',
        'match item_type:\n    case "study":\n        pass\n',
    ],
)
def test_guard_spots_a_comparison_with_a_type_name(source: str) -> None:
    assert lines_comparing_against(source, set(PHASE_1_TYPE_NAMES))


@pytest.mark.parametrize(
    "source",
    [
        'content_type = CONTENT_TYPES.get("movie")\n',
        'if media_type == "reel":\n    pass\n',
        "if content_type.resolver is not None:\n    pass\n",
    ],
)
def test_guard_allows_code_that_reads_the_registry(source: str) -> None:
    assert lines_comparing_against(source, set(PHASE_1_TYPE_NAMES)) == []


def test_no_code_outside_the_type_modules_branches_on_a_type_name() -> None:
    type_names = {content_type.name for content_type in CONTENT_TYPES.all()}
    violations = [
        f"{path.relative_to(APP_DIR.parent)}:{line}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if not any(path.is_relative_to(allowed) for allowed in CONTENT_TYPE_LOGIC_DIRS)
        for line in lines_comparing_against(path.read_text(encoding="utf-8"), type_names)
    ]

    assert violations == [], (
        "Branching on a content type's name belongs in app/types/ or app/resolvers/. "
        "Read the type's declaration from the registry instead."
    )
