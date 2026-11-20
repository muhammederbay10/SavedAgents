"""Filter normalization: map what the user wrote to a value that exists in their saves.

"beşilktaş" → Beşiktaş, "netflx" → Netflix, "bruklin" → Brooklyn
(ARCHITECTURE §3.4 and §3.5, decision record 0005). For each wording:

1. **Exact hit, no model call.** The wording, in canonical form, equals a
   value in the user's own saves, or a canonical value or *verified* alias of
   the wording's vocabulary field. Candidate and rejected aliases never count.
2. **Options.** Otherwise the user's own values are ranked: by meaning, through
   the vocabulary's embeddings, and by spelling. Values the wording was
   rejected as an alias of are left out. A near value is only an option.
3. **Decision.** Gemini Decision 1 picks one option or "none of these". Only a
   pick at or above `FILTER_NORMALIZATION_MIN_PROBABILITY` is used.

When nothing matches, the wording is kept as written. The filter then finds
nothing, and the fallback ladder relaxes it (L2). Normalization never writes:
it never adds a value or an alias to the shared vocabulary.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from itertools import chain, zip_longest
from typing import Final

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.search.prompts import RetrievalPrompts
from app.agent.search.query import Wording
from app.agent.search.saved_values import (
    ValueScope,
    nearest_vocabulary_values,
    saved_values,
    vocabulary_values,
)
from app.config.retrieval import RetrievalSettings
from app.models.base import ChoiceAnswer, ChoiceQuestion, Classifier, Embedder
from app.models.errors import ModelError
from app.pipeline.vocabulary.store import canonical_form, find_exact, rejected_value_ids

logger = logging.getLogger(__name__)

_QUESTION_KEY: Final = "value"
_NO_MATCH_OPTION: Final = "none"
_OPTION_PREFIX: Final = "value_"


class NormalizationMethod(StrEnum):
    """How a wording became the value a filter uses.

    Attributes:
        SAVED_VALUE: It equals a value in the user's saves (exact hit).
        VOCABULARY: It equals a canonical value or a verified alias (exact hit).
        DECISION: The decision model picked one of the user's values.
        UNCHANGED: Nothing matched; the wording is used as written.
    """

    SAVED_VALUE = "saved_value"
    VOCABULARY = "vocabulary"
    DECISION = "decision"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class NormalizedWording:
    """The value a filter uses for one wording, and how it was found.

    Attributes:
        slot: Where the wording sits in the query, e.g. `filters.area`.
        wording: What the user wrote.
        value: The value the filter uses.
        method: How the value was found.
        probability: The decision model's probability for the value; `None`
            unless the decision model picked it.
    """

    slot: str
    wording: str = field(repr=False)
    value: str = field(repr=False)
    method: NormalizationMethod
    probability: float | None = None


@dataclass(frozen=True)
class _Pending:
    """A wording with no exact hit, and the options offered for it."""

    wording: Wording
    options: tuple[str, ...]


class FilterNormalizer:
    """Maps the wordings of a search to values in the asking user's own saves."""

    def __init__(
        self,
        *,
        classifier: Classifier,
        embedder: Embedder,
        prompts: RetrievalPrompts,
        settings: RetrievalSettings,
    ) -> None:
        """Create the normalizer.

        Args:
            classifier: The decision model that picks among the options.
            embedder: The embedding model that ranks vocabulary values by meaning.
            prompts: The wording sent to the decision model.
            settings: The probability threshold and the most options per wording.
        """
        self._classifier = classifier
        self._embedder = embedder
        self._prompts = prompts
        self._settings = settings

    async def normalize(
        self, session: AsyncSession, scope: ValueScope, wordings: Sequence[Wording]
    ) -> dict[str, NormalizedWording]:
        """Find the value each wording stands for.

        Args:
            session: The session to read the user's values in.
            scope: Whose saves the values come from.
            wordings: The wordings of one search.

        Returns:
            One normalized wording per slot.
        """
        normalized: dict[str, NormalizedWording] = {}
        candidates: list[tuple[Wording, list[str]]] = []
        for wording in wordings:
            values = await saved_values(session, scope, wording)
            hit = await self._exact_hit(session, wording, values)
            if hit is None:
                candidates.append((wording, values))
            else:
                normalized[wording.slot] = hit
        pending = await self._with_options(session, candidates)
        decided = await asyncio.gather(*(self._decide(item) for item in pending))
        normalized.update((result.slot, result) for result in decided)
        return normalized

    async def _exact_hit(
        self, session: AsyncSession, wording: Wording, values: Sequence[str]
    ) -> NormalizedWording | None:
        form = canonical_form(wording.wording)
        for value in values:
            if canonical_form(value) == form:
                return _result(wording, value, NormalizationMethod.SAVED_VALUE)
        if wording.vocabulary is None:
            return None
        hit = await find_exact(session, wording.vocabulary, form)
        if hit is None:
            return None
        return _result(wording, hit.value, NormalizationMethod.VOCABULARY)

    async def _with_options(
        self, session: AsyncSession, candidates: Sequence[tuple[Wording, list[str]]]
    ) -> list[_Pending]:
        embeddings = await self._embed_vocabulary_wordings(
            [wording for wording, values in candidates if values]
        )
        pending: list[_Pending] = []
        for wording, values in candidates:
            options = await self._options(session, wording, values, embeddings.get(wording.slot))
            pending.append(_Pending(wording=wording, options=tuple(options)))
        return pending

    async def _embed_vocabulary_wordings(
        self, wordings: Sequence[Wording]
    ) -> dict[str, tuple[float, ...]]:
        in_vocabulary = [wording for wording in wordings if wording.vocabulary is not None]
        if not in_vocabulary:
            return {}
        try:
            result = await self._embedder.embed([wording.wording for wording in in_vocabulary])
        except ModelError as error:
            # Ranking by spelling alone still offers every value the decision model needs.
            logger.warning(
                "filter_normalization_embedding_failed",
                extra={"error": type(error).__name__, "model": error.model},
            )
            return {}
        return {
            wording.slot: vector
            for wording, vector in zip(in_vocabulary, result.vectors, strict=True)
        }

    async def _options(
        self,
        session: AsyncSession,
        wording: Wording,
        values: Sequence[str],
        embedding: tuple[float, ...] | None,
    ) -> list[str]:
        if not values:
            return []
        limit = self._settings.filter_normalization_max_options
        form = canonical_form(wording.wording)
        excluded_forms: set[str] = set()
        if wording.vocabulary is not None:
            rejected_ids = await rejected_value_ids(session, wording.vocabulary, form)
            rejected = await vocabulary_values(session, rejected_ids)
            excluded_forms = {canonical_form(value) for value in rejected}
        # Saved values are compared in the same form as the vocabulary's, so "Restaurant"
        # or " restaurant " is left out when the wording was rejected for "restaurant".
        allowed: dict[str, list[str]] = {}
        for value in values:
            value_form = canonical_form(value)
            if value_form not in excluded_forms:
                allowed.setdefault(value_form, []).append(value)
        by_meaning: list[str] = []
        if wording.vocabulary is not None and embedding is not None:
            nearest_forms = await self._rank_by_meaning(
                session, wording.vocabulary, wording.slot, embedding, list(allowed), limit
            )
            by_meaning = [value for value_form in nearest_forms for value in allowed[value_form]]
        by_spelling = sorted(
            (value for stored in allowed.values() for value in stored),
            key=lambda value: (-_spelling_similarity(form, value), value),
        )
        return _interleave(by_meaning, by_spelling, limit)

    async def _rank_by_meaning(
        self,
        session: AsyncSession,
        field: str,
        slot: str,
        embedding: tuple[float, ...],
        among_forms: Sequence[str],
        limit: int,
    ) -> list[str]:
        """Rank vocabulary values by meaning, or return none if the database fails it.

        Ranking by meaning only adds options; ranking by spelling still offers
        every allowed value. The query runs in a savepoint, so a failure leaves
        the session usable for the rest of the search.
        """
        try:
            async with session.begin_nested():
                return await nearest_vocabulary_values(
                    session, field, embedding, among=among_forms, limit=limit
                )
        except DBAPIError as error:
            logger.warning(
                "filter_normalization_ranking_failed",
                extra={"slot": slot, "error": type(error.orig).__name__},
            )
            return []

    async def _decide(self, pending: _Pending) -> NormalizedWording:
        wording = pending.wording
        if not pending.options:
            return _result(wording, wording.wording, NormalizationMethod.UNCHANGED)
        options = {f"{_OPTION_PREFIX}{index}": value for index, value in enumerate(pending.options)}
        filter_name = _filter_name(wording)
        question = ChoiceQuestion(
            key=_QUESTION_KEY,
            instructions=self._prompts.normalization_question.format(filter=filter_name),
            options={**options, _NO_MATCH_OPTION: self._prompts.normalization_no_match_option},
        )
        state = self._prompts.normalization_input.format(
            filter=filter_name, wording=wording.wording
        )
        try:
            decision = await self._classifier.decide([state], [question])
        except ModelError as error:
            logger.warning(
                "filter_normalization_failed",
                extra={"slot": wording.slot, "error": type(error).__name__, "model": error.model},
            )
            return _result(wording, wording.wording, NormalizationMethod.UNCHANGED)
        return self._read_answer(wording, options, decision.answers.get(_QUESTION_KEY))

    def _read_answer(
        self, wording: Wording, options: dict[str, str], answer: object
    ) -> NormalizedWording:
        result = _result(wording, wording.wording, NormalizationMethod.UNCHANGED)
        if isinstance(answer, ChoiceAnswer) and answer.choice in options:
            probability = answer.probabilities.get(answer.choice, 0.0)
            if probability >= self._settings.filter_normalization_min_probability:
                result = _result(
                    wording, options[answer.choice], NormalizationMethod.DECISION, probability
                )
        logger.info(
            "filter_normalized",
            extra={
                "slot": wording.slot,
                "method": result.method,
                "probability": result.probability,
                "prompt_version": self._prompts.version,
                "option_count": len(options),
            },
        )
        return result


def _result(
    wording: Wording, value: str, method: NormalizationMethod, probability: float | None = None
) -> NormalizedWording:
    return NormalizedWording(
        slot=wording.slot,
        wording=wording.wording,
        value=value,
        method=method,
        probability=probability,
    )


def _filter_name(wording: Wording) -> str:
    if wording.search_filter is not None:
        return wording.search_filter.name
    return wording.vocabulary or wording.slot


def _spelling_similarity(form: str, value: str) -> float:
    return SequenceMatcher(None, form, canonical_form(value)).ratio()


def _interleave(first: Sequence[str], second: Sequence[str], limit: int) -> list[str]:
    """Merge two rankings, taking from each in turn, without repeats, up to `limit`."""
    merged: list[str] = []
    for value in chain.from_iterable(zip_longest(first, second)):
        if value is not None and value not in merged:
            merged.append(value)
        if len(merged) == limit:
            break
    return merged
