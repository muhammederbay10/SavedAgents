"""The code checks of the privacy gate read characters, so they work for any language."""

import pytest

from app.pipeline.normalize import fails_privacy_checks
from app.pipeline.vocabulary import canonical_form

MAX_LENGTH = 40
PHONE_MIN_DIGITS = 7


def fails(wording: str) -> bool:
    return fails_privacy_checks(wording, max_length=MAX_LENGTH, phone_min_digits=PHONE_MIN_DIGITS)


@pytest.mark.parametrize(
    "wording",
    [
        "korean barbecue",
        "Kore mutfağı",
        "ラーメン",
        "مطعم شعبي",
        "top 10 list",
        "node.js tutorial",
        "1990s movies",
        "c++",
    ],
)
def test_generic_wording_passes_the_code_checks(wording: str) -> None:
    assert not fails(wording)


@pytest.mark.parametrize(
    "wording",
    [
        "@ayse.cooks recipes",
        "ask me at someone@example.test",
        "https://example.test/menu",
        "see www.example.test",
        "example.test/menu",
        "call +90 532 000 00 00",
        "(212) 555-0100",
        "\u0660\u0665\u0663\u0662 \u0660\u0660\u0660 \u0660\u0660 \u0660\u0660",
        "x" * (MAX_LENGTH + 1),
    ],
    ids=[
        "handle",
        "email",
        "url",
        "www",
        "domain-path",
        "phone",
        "phone-brackets",
        "arabic-indic-digits",
        "too-long",
    ],
)
def test_personal_or_long_wording_fails_the_code_checks(wording: str) -> None:
    assert fails(wording)


@pytest.mark.parametrize(
    ("wording", "form"),
    [
        ("  Korean   Food ", "korean food"),
        ("KORE MUTFAĞI", "kore mutfaği"),
        ("Ramen\tShop", "ramen shop"),
    ],
)
def test_canonical_form_is_trimmed_single_spaced_and_lower_case(wording: str, form: str) -> None:
    assert canonical_form(wording) == form
