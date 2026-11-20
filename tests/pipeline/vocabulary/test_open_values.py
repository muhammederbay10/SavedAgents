"""Open values: their paths in an extraction, and how an extraction becomes an item's columns."""

import pytest

from app.pipeline.vocabulary.open_values import (
    OpenValue,
    document_of_item,
    find_open_values,
    item_values,
    set_value,
    vocabulary_pairs,
)
from app.types.base import Extraction
from app.types.registry import CONTENT_TYPES
from tests.pipeline.support import open_extraction, place_extraction, study_extraction

PLACE_SCHEMA = CONTENT_TYPES.extraction_schema("place")
STUDY_SCHEMA = CONTENT_TYPES.extraction_schema("study")


def test_every_open_value_of_a_list_post_has_its_own_path() -> None:
    document = place_extraction(("Kronotrop", "Specialty Coffee"), ("Mikla", None), tags=("a", "b"))

    found = find_open_values(PLACE_SCHEMA, document)

    assert found == [
        OpenValue("category", "category", "Restaurant", nested=False),
        OpenValue("tags.0", "tag", "a", nested=False),
        OpenValue("tags.1", "tag", "b", nested=False),
        OpenValue("entities.0.cuisine", "cuisine", "Specialty Coffee", nested=True),
    ]


def test_type_fields_marked_for_the_vocabulary_are_open_values() -> None:
    found = {
        value.path: value.field for value in find_open_values(STUDY_SCHEMA, study_extraction())
    }

    assert found == {
        "category": "category",
        "tags.0": "tag",
        "subject": "subject",
        "format": "format",
    }


def test_names_titles_and_topics_are_not_open_values() -> None:
    paths = [value.path for value in find_open_values(PLACE_SCHEMA, place_extraction(("X", None)))]

    assert "entities.0.name" not in paths


def test_set_value_replaces_a_value_at_a_path() -> None:
    document = place_extraction(("Kronotrop", "Kahve"))

    set_value(document, "entities.0.cuisine", "coffee")
    set_value(document, "tags.0", "rooftop")

    assert document["entities"][0]["cuisine"] == "coffee"  # type: ignore[index]
    assert document["tags"] == ["rooftop"]


def test_set_value_on_a_missing_path_fails() -> None:
    with pytest.raises(KeyError):
        set_value(open_extraction(tags=()), "tags.0", "x")


def test_nested_open_values_are_listed_at_the_top_of_fields() -> None:
    document = place_extraction(("A", "korean"), ("B", "coffee"), ("C", "korean"))

    values = item_values(PLACE_SCHEMA, document)

    assert values.category == "Restaurant"
    assert values.tags == ["Rooftop"]
    assert values.fields["cuisine"] == ["korean", "coffee"]
    assert [entity["name"] for entity in values.fields["entities"]] == ["A", "B", "C"]  # type: ignore[union-attr]
    assert "summary" not in values.fields


def test_an_item_without_a_built_in_type_has_empty_fields() -> None:
    values = item_values(Extraction, open_extraction())

    assert values.fields == {}
    assert values.category == "Car Review"


def test_an_item_rebuilds_its_extraction_without_the_listed_values() -> None:
    document = place_extraction(("A", "korean"))
    values = item_values(PLACE_SCHEMA, document)

    rebuilt = document_of_item(
        PLACE_SCHEMA,
        summary=values.summary,
        category=values.category,
        tags=values.tags,
        fields=values.fields,
    )

    assert rebuilt == document


def test_vocabulary_pairs_follow_the_deletion_rule() -> None:
    pairs = vocabulary_pairs(
        category="restaurant",
        subtype=None,
        tags=["rooftop", "rooftop"],
        fields={"cuisine": ["korean", "korean"], "subject": "maths", "entities": [{"name": "A"}]},
    )

    assert pairs == {
        ("category", "restaurant"),
        ("tag", "rooftop"),
        ("cuisine", "korean"),
        ("subject", "maths"),
    }
