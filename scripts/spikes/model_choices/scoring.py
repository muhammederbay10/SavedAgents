"""Word and character error rates, with one text normalization for every model.

The rules are the ones behind the numbers in `docs/decisions/0008-model-choices.md`:

1. Unicode NFKC.
2. Case folding. Turkish first lower-cases its dotless and dotted capital I the
   Turkish way, which plain case folding gets wrong.
3. Punctuation, symbols and separators become spaces; then spaces are collapsed.

Accents, combining marks and digits are kept. Numbers are not spelled out and
nothing is transliterated. The word error rate (WER) counts space-separated
words. Japanese uses the character error rate (CER) over code points with the
spaces removed.
"""

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from scripts.spikes.model_choices.settings import (
    CHARACTER_METRIC_LANGUAGES,
    TURKISH_CASING_LANGUAGES,
)

Metric = Literal["WER", "CER"]

# First letter of the Unicode general category: punctuation, symbol, separator.
_CATEGORIES_REPLACED_BY_SPACE = frozenset({"P", "S", "Z"})
_TURKISH_UPPER_TO_LOWER = str.maketrans(
    {
        "I": "\N{LATIN SMALL LETTER DOTLESS I}",
        "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}": "i",
    }
)
_PERCENT = 100.0


@dataclass(frozen=True)
class ErrorCount:
    """Edit errors of one or more transcripts against their references.

    Attributes:
        edit_errors: Substitutions, deletions and insertions.
        reference_units: Words (or characters, for CER) in the references.
    """

    edit_errors: int
    reference_units: int

    @property
    def error_percent(self) -> float:
        """Return the error rate in percent.

        Raises:
            ValueError: If there are no reference units to divide by.
        """
        if self.reference_units == 0:
            raise ValueError("an error rate needs at least one reference unit")
        return _PERCENT * self.edit_errors / self.reference_units


def metric_for(language: str) -> Metric:
    """Return the metric used for a FLEURS language code."""
    return "CER" if language in CHARACTER_METRIC_LANGUAGES else "WER"


def normalize_text(text: str, language: str) -> str:
    """Normalize a reference or a transcript before scoring.

    Args:
        text: The raw text.
        language: The FLEURS language code, e.g. `tr_tr`.

    Returns:
        The normalized text, with single spaces between words.
    """
    composed = unicodedata.normalize("NFKC", text)
    if language in TURKISH_CASING_LANGUAGES:
        composed = composed.translate(_TURKISH_UPPER_TO_LOWER)
    folded = composed.casefold()
    spaced = "".join(
        " " if unicodedata.category(character)[0] in _CATEGORIES_REPLACED_BY_SPACE else character
        for character in folded
    )
    return " ".join(spaced.split())


def scoring_units(text: str, language: str) -> list[str]:
    """Split a text into the units its metric counts: words, or characters for CER."""
    normalized = normalize_text(text, language)
    if metric_for(language) == "CER":
        return list(normalized.replace(" ", ""))
    return normalized.split()


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Return the Levenshtein distance between two sequences of units."""
    previous_row = list(range(len(hypothesis) + 1))
    for reference_index, reference_unit in enumerate(reference, start=1):
        current_row = [reference_index]
        for hypothesis_index, hypothesis_unit in enumerate(hypothesis, start=1):
            substitution = previous_row[hypothesis_index - 1] + (reference_unit != hypothesis_unit)
            deletion = previous_row[hypothesis_index] + 1
            insertion = current_row[hypothesis_index - 1] + 1
            current_row.append(min(substitution, deletion, insertion))
        previous_row = current_row
    return previous_row[-1]


def score_transcript(reference: str, hypothesis: str, language: str) -> ErrorCount:
    """Score one transcript against its reference.

    An empty transcript scores as every reference unit deleted, so a model that
    returns nothing is not left out of the error rate.

    Args:
        reference: The reference sentence.
        hypothesis: The model's transcript; empty when the request failed.
        language: The FLEURS language code.

    Returns:
        The edit errors and the number of reference units.
    """
    reference_units = scoring_units(reference, language)
    hypothesis_units = scoring_units(hypothesis, language)
    return ErrorCount(edit_distance(reference_units, hypothesis_units), len(reference_units))


def combine(counts: Iterable[ErrorCount]) -> ErrorCount:
    """Add up error counts, so the rate is errors over all reference units (micro average)."""
    edit_errors = 0
    reference_units = 0
    for count in counts:
        edit_errors += count.edit_errors
        reference_units += count.reference_units
    return ErrorCount(edit_errors, reference_units)
