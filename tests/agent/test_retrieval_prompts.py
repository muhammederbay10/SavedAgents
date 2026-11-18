from dataclasses import replace

import pytest

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
