"""The prompt container refuses blank wording and wrong placeholders when it is built."""

import dataclasses
from pathlib import Path

import pytest

from app.pipeline.prompts import PIPELINE_PROMPTS
from tests.pipeline.support import TEST_PROMPTS

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"


def test_the_test_prompts_are_valid() -> None:
    assert TEST_PROMPTS.version


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("classify_type_question", "  "),
        ("classify_input", "caption={caption}"),
        ("classify_input", "caption={caption} collection={collection} extra={extra}"),
        ("vocabulary_input", "{field!r} {wording}"),
        ("vocabulary_input", "{field:>10} {wording}"),
        ("vocabulary_input", "{field} {wording} {"),
        ("extraction_system", "a {placeholder} in a fixed text"),
        ("vocabulary_input", "{0} {wording} {field}"),
    ],
)
def test_wrong_wording_is_refused_when_built(field: str, text: str) -> None:
    with pytest.raises(ValueError, match=field):
        dataclasses.replace(TEST_PROMPTS, **{field: text})


def test_post_text_with_braces_is_only_ever_a_value() -> None:
    caption = "{collection} {0} {caption!r}"

    text = TEST_PROMPTS.classify_input.format(caption=caption, collection="Food")

    assert text == "caption={collection} {0} {caption!r}\ncollection=Food"


def test_the_approved_prompts_are_valid_and_versioned() -> None:
    assert PIPELINE_PROMPTS.version == "2026-11-18.1"


def test_the_privacy_gate_asks_the_question_from_the_architecture_word_for_word() -> None:
    assert PIPELINE_PROMPTS.privacy_gate_question in ARCHITECTURE.read_text(encoding="utf-8")


def test_the_match_question_names_the_field_and_carries_no_wording() -> None:
    question = PIPELINE_PROMPTS.vocabulary_match_question.format(field="cuisine")

    assert question.startswith("A saved post's cuisine is written as the wording below.")
