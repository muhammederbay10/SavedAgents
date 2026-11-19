from dataclasses import replace

import pytest

from app.agent.prompts import RETRIEVAL_PROMPTS
from tests.agent.retrieval_support import TEST_PROMPTS


@pytest.mark.parametrize(
    ("field", "template"),
    [
        ("normalization_question", "Which value does the user mean?"),
        ("normalization_question", "Which {filter} for {wording}?"),
        ("normalization_input", "{filter}"),
        ("normalization_input", "{filter!r}: {wording}"),
        ("normalization_input", "{filter}: {wording"),
        ("normalization_no_match_option", "None of {these}."),
        ("version", " "),
    ],
)
def test_prompts_with_wrong_placeholders_or_blank_wording_are_rejected(
    field: str, template: str
) -> None:
    with pytest.raises(ValueError, match=f"prompt {field}"):
        replace(TEST_PROMPTS, **{field: template})


def test_the_users_wording_is_only_ever_a_value() -> None:
    rendered = TEST_PROMPTS.normalization_input.format(filter="area", wording="{filter} {0}")

    assert rendered == "area: {filter} {0}"


def test_the_approved_wording_is_used_word_for_word() -> None:
    assert RETRIEVAL_PROMPTS.version == "2026-11-18.1"
    assert RETRIEVAL_PROMPTS.normalization_question == (
        "The user is searching their own saved posts and wrote this {filter} as a filter, in "
        "their own words, maybe with a typo, another spelling or another language. Which of the "
        "saved values names the same thing? Pick 'none of these' unless one clearly does."
    )
    assert RETRIEVAL_PROMPTS.normalization_no_match_option == (
        "None of these values names what the user wrote."
    )
    assert RETRIEVAL_PROMPTS.normalization_input == "Filter: {filter}\nThe user wrote: {wording}"
    rendered = RETRIEVAL_PROMPTS.normalization_input.format(filter="area", wording="beşilktaş")
    assert rendered.splitlines() == ["Filter: area", "The user wrote: beşilktaş"]
