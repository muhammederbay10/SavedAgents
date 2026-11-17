import pytest

from scripts.spikes.model_choices.scoring import (
    ErrorCount,
    combine,
    edit_distance,
    metric_for,
    normalize_text,
    score_transcript,
)


def test_punctuation_and_case_do_not_count_as_errors() -> None:
    score = score_transcript(
        "Two pools: hidden and expressed.", "two pools hidden AND expressed", "en_us"
    )
    assert score == ErrorCount(edit_errors=0, reference_units=5)


def test_turkish_capital_i_is_lowercased_the_turkish_way() -> None:
    dotless = "\N{LATIN SMALL LETTER DOTLESS I}"
    dotted_capital = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
    assert (
        normalize_text(f"ISI {dotted_capital}Y{dotted_capital}", "tr_tr")
        == f"{dotless}s{dotless} iyi"
    )


def test_other_languages_use_plain_case_folding() -> None:
    assert normalize_text("ISI", "en_us") == "isi"


def test_fullwidth_characters_are_normalized() -> None:
    fullwidth = "\uff21\uff22\uff23\u3000\uff11\uff12\uff13"
    assert normalize_text(fullwidth, "en_us") == "abc 123"


def test_accents_and_digits_are_kept() -> None:
    assert normalize_text("Café, 24 años!", "es_419") == "café 24 años"


def test_word_error_rate_counts_substitutions_deletions_and_insertions() -> None:
    assert edit_distance(["a", "b", "c", "d"], ["a", "x", "c"]) == 2
    assert edit_distance(["a", "b"], ["a", "b", "c"]) == 1


def test_japanese_is_scored_per_character_without_spaces() -> None:
    assert metric_for("ja_jp") == "CER"
    score = score_transcript("警察は、言った。", "警察 が言った", "ja_jp")
    assert score == ErrorCount(edit_errors=1, reference_units=6)


def test_empty_transcript_counts_every_reference_word_as_deleted() -> None:
    assert score_transcript("one two three", "", "en_us") == ErrorCount(3, 3)


def test_combined_rate_is_errors_over_all_reference_units() -> None:
    combined = combine([ErrorCount(1, 10), ErrorCount(3, 30), ErrorCount(0, 10)])
    assert combined == ErrorCount(4, 50)
    assert combined.error_percent == pytest.approx(8.0)


def test_error_rate_without_reference_units_raises() -> None:
    with pytest.raises(ValueError, match="reference unit"):
        _ = ErrorCount(0, 0).error_percent
