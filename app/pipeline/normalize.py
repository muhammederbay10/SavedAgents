"""Normalize open values against the shared vocabulary (`docs/ARCHITECTURE.md` §3.4, record 0005).

For each open value, in this order:

1. **Exact hit, no model call:** the wording's canonical form equals a canonical
   value or a *verified* alias of the same field. This is the only shortcut;
   candidate and rejected aliases never count.
2. **Privacy gate**, before anything can touch the shared vocabulary: code
   checks (an @handle or email address, a link, a phone-like number, or more
   than `VOCAB_MAX_VALUE_LENGTH` characters), then a decision-model yes/no that
   passes only a confident "generic" (`VOCAB_PRIVACY_MIN_PROBABILITY`).
   Uncertain counts as no. Wording that fails stays on the user's own item, as
   written, and is searchable only by that user.
3. **Match or new:** the nearest values of the same field by embedding are
   only candidates. The decision model picks one of them or "new". A match uses
   that value and stores the wording as a *candidate* alias; "new" adds the
   wording's canonical form as a value. Values the wording was rejected as an
   alias of are never offered again.
4. A candidate alias becomes *verified* once `VOCAB_ALIAS_MIN_USERS` different
   users matched through it with at least `VOCAB_ALIAS_MIN_PROBABILITY`.

Every value's outcome, with the value and alias it went through, is recorded
in the item's normalize step (`NormalizedValue.provenance`). Nothing here logs
wording: logs carry fields and outcomes only.

Rejecting an alias (`app.pipeline.renormalize`) normalizes again only the
items that matched through it.
"""

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.base import (
    ChoiceAnswer,
    ChoiceQuestion,
    Classifier,
    Embedder,
    YesNoAnswer,
    YesNoQuestion,
)
from app.pipeline import vocabulary
from app.pipeline.costs import CallCosts
from app.pipeline.open_values import OpenValue
from app.pipeline.prompts import PipelinePrompts

logger = logging.getLogger(__name__)

PRIVACY_QUESTION_KEY: Final = "generic"
MATCH_QUESTION_KEY: Final = "same_value"
NEW_VALUE_OPTION: Final = "new"
_CANDIDATE_OPTION_PREFIX: Final = "value_"
RAW_WORDING: Final = "wording"
RAW_FIELD: Final = "field"
RAW_USER_SET: Final = "user_set"
PROVENANCE_VALUES: Final = "values"
PROVENANCE_ALIAS_ID: Final = "alias_id"

# Code checks of the privacy gate. They read characters, not words of any language.
_HANDLE_OR_EMAIL = re.compile(r"@\w")
_LINK = re.compile(r"[a-z][a-z0-9+.\-]*://|(?<!\w)www\.|\w\.\w{2,}/", re.IGNORECASE)


class NormalizeOutcome(StrEnum):
    """How one value was normalized."""

    EXACT_VALUE = "exact_value"
    EXACT_ALIAS = "exact_alias"
    MATCHED = "matched"
    NEW = "new"
    PRIVATE = "private"
    UNDECIDED = "undecided"


@dataclass(frozen=True)
class _MatchDecision:
    """The decision model's answer to match-or-new.

    Attributes:
        candidate: The existing value it picked; `None` for "new" and when it
            did not answer.
        probability: The probability of the picked option.
        answered: Whether it answered at all.
    """

    candidate: vocabulary.Candidate | None
    probability: float | None
    answered: bool


@dataclass(frozen=True)
class VocabularyRules:
    """The thresholds of normalization, from `PipelineSettings`.

    Attributes:
        privacy_min_probability: `VOCAB_PRIVACY_MIN_PROBABILITY`.
        alias_min_probability: `VOCAB_ALIAS_MIN_PROBABILITY`.
        alias_min_users: `VOCAB_ALIAS_MIN_USERS`.
        max_value_length: `VOCAB_MAX_VALUE_LENGTH`.
        phone_min_digits: `VOCAB_PHONE_MIN_DIGITS`.
        match_candidates: `VOCAB_MATCH_CANDIDATES`.
    """

    privacy_min_probability: float
    alias_min_probability: float
    alias_min_users: int
    max_value_length: int
    phone_min_digits: int
    match_candidates: int


@dataclass(frozen=True)
class NormalizedValue:
    """What one open value became.

    Attributes:
        value: The value the item uses: a canonical value, or the wording as
            written when it stays on the item only.
        outcome: How it was normalized.
        vocabulary_id: The canonical value it uses, if any.
        alias_id: The alias it went through, if any.
        probability: The decision model's probability for a match.
    """

    value: str = field(repr=False)
    outcome: NormalizeOutcome
    vocabulary_id: uuid.UUID | None = None
    alias_id: uuid.UUID | None = None
    probability: float | None = None

    def provenance(self, vocabulary_field: str) -> dict[str, object]:
        """Return the record of this value for the item's normalize step.

        Args:
            vocabulary_field: The vocabulary field of the value.

        Returns:
            A JSON object with the field, outcome, ids and probability. The
            wording itself is kept in the item's `raw_values`.
        """
        return {
            "field": vocabulary_field,
            "outcome": self.outcome,
            "vocabulary_id": str(self.vocabulary_id) if self.vocabulary_id else None,
            "alias_id": str(self.alias_id) if self.alias_id else None,
            "probability": self.probability,
        }


@dataclass(frozen=True)
class Normalization:
    """The result of normalizing some open values.

    Attributes:
        values: What each value became, by its path.
        costs: The charges of the decision and embedding calls.
    """

    values: dict[str, NormalizedValue]
    costs: CallCosts

    def confident_alias_ids(self, min_probability: float) -> set[uuid.UUID]:
        """Return the aliases matched with enough confidence to count towards verification.

        Args:
            min_probability: `VOCAB_ALIAS_MIN_PROBABILITY`.

        Returns:
            Their ids.
        """
        return {
            value.alias_id
            for value in self.values.values()
            if value.outcome is NormalizeOutcome.MATCHED
            and value.alias_id is not None
            and value.probability is not None
            and value.probability >= min_probability
        }


def fails_privacy_checks(wording: str, *, max_length: int, phone_min_digits: int) -> bool:
    """Run the code checks of the privacy gate.

    Args:
        wording: The wording, as written.
        max_length: The longest wording that may be shared, in characters.
        phone_min_digits: The digits in a row, allowing separators between
            them, from which wording looks like a phone number.

    Returns:
        True if the wording has an @handle or email address, a link, a
        phone-like number, or is too long.
    """
    stripped = wording.strip()
    return (
        len(stripped) > max_length
        or _HANDLE_OR_EMAIL.search(stripped) is not None
        or _LINK.search(stripped) is not None
        or _phone_like(phone_min_digits).search(stripped) is not None
    )


def raw_values_of(open_values: Sequence[OpenValue]) -> dict[str, object]:
    """Return the `saved_item.raw_values` that keep an item's own wording.

    Args:
        open_values: The extraction's open values before normalization.

    Returns:
        Each value's field and wording by its path, none of them set by hand.
    """
    return {
        open_value.path: {
            RAW_FIELD: open_value.field,
            RAW_WORDING: open_value.wording,
            RAW_USER_SET: False,
        }
        for open_value in open_values
    }


class Normalizer:
    """Normalizes open values against the shared vocabulary."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        classifier: Classifier,
        embedder: Embedder,
        prompts: PipelinePrompts,
        rules: VocabularyRules,
    ) -> None:
        """Create the normalizer.

        Args:
            sessions: Opens a database session. Vocabulary changes are
                committed at once, independent of any item.
            classifier: The decision model, for the gate and match-or-new.
            embedder: The embedding model, for candidates.
            prompts: The wording.
            rules: The thresholds.
        """
        self._sessions = sessions
        self._classifier = classifier
        self._embedder = embedder
        self._prompts = prompts
        self._rules = rules

    async def normalize(self, open_values: Sequence[OpenValue]) -> Normalization:
        """Normalize open values; the same wording in the same field is decided once.

        Args:
            open_values: The values, with their paths.

        Returns:
            What each value became, by its path.

        Raises:
            ModelError: If a decision or embedding call failed.
            sqlalchemy.exc.SQLAlchemyError: If the database failed.
        """
        decided: dict[tuple[str, str], NormalizedValue] = {}
        values: dict[str, NormalizedValue] = {}
        costs = CallCosts()
        for open_value in open_values:
            key = (open_value.field, vocabulary.canonical_form(open_value.wording))
            if key not in decided:
                decided[key], call_costs = await self._normalize_one(
                    open_value.field, open_value.wording
                )
                costs = costs + call_costs
            values[open_value.path] = decided[key]
        _log_outcomes(open_values, values)
        return Normalization(values=values, costs=costs)

    async def verify_aliases(self, alias_ids: set[uuid.UUID]) -> None:
        """Verify each candidate alias that enough different users matched through.

        Call it after the items' normalize steps are committed, so they count.

        Args:
            alias_ids: Aliases that were just matched with enough confidence.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database failed.
        """
        for alias_id in sorted(alias_ids):
            async with self._sessions() as session, session.begin():
                users = await vocabulary.confident_user_count(
                    session, alias_id, self._rules.alias_min_probability
                )
                if users >= self._rules.alias_min_users and await vocabulary.verify_alias(
                    session, alias_id
                ):
                    logger.info("vocabulary_alias_verified", extra={"alias_id": str(alias_id)})

    async def _normalize_one(
        self, vocabulary_field: str, wording: str
    ) -> tuple[NormalizedValue, CallCosts]:
        form = vocabulary.canonical_form(wording)
        async with self._sessions() as session:
            exact = await vocabulary.find_exact(session, vocabulary_field, form)
        if exact is not None:
            outcome = (
                NormalizeOutcome.EXACT_VALUE
                if exact.alias_id is None
                else NormalizeOutcome.EXACT_ALIAS
            )
            normalized = NormalizedValue(exact.value, outcome, exact.vocabulary_id, exact.alias_id)
            return normalized, CallCosts()
        is_generic, costs = await self._passes_privacy_gate(vocabulary_field, wording)
        if not is_generic:
            return NormalizedValue(wording.strip(), NormalizeOutcome.PRIVATE), costs
        embedded = await self._embedder.embed([form])
        costs = costs.add(embedded.cost_usd)
        embedding = list(embedded.vectors[0])
        async with self._sessions() as session:
            excluded = await vocabulary.rejected_value_ids(session, vocabulary_field, form)
            candidates = await vocabulary.nearest_values(
                session,
                vocabulary_field,
                embedding,
                limit=self._rules.match_candidates,
                excluded_ids=excluded,
            )
        if candidates:
            decision, decision_costs = await self._match_or_new(
                vocabulary_field, wording, candidates
            )
            costs = costs + decision_costs
            if not decision.answered:
                return NormalizedValue(wording.strip(), NormalizeOutcome.UNDECIDED), costs
            if decision.candidate is not None:
                return await self._record_match(form, decision), costs
        async with self._sessions() as session, session.begin():
            vocabulary_id = await vocabulary.add_value(session, vocabulary_field, form, embedding)
        return NormalizedValue(form, NormalizeOutcome.NEW, vocabulary_id), costs

    async def _passes_privacy_gate(
        self, vocabulary_field: str, wording: str
    ) -> tuple[bool, CallCosts]:
        if fails_privacy_checks(
            wording,
            max_length=self._rules.max_value_length,
            phone_min_digits=self._rules.phone_min_digits,
        ):
            return False, CallCosts()
        result = await self._classifier.decide(
            [self._prompts.vocabulary_input.format(field=vocabulary_field, wording=wording)],
            [
                YesNoQuestion(
                    key=PRIVACY_QUESTION_KEY, instructions=self._prompts.privacy_gate_question
                )
            ],
        )
        answer = result.answers[PRIVACY_QUESTION_KEY]
        is_generic = (
            isinstance(answer, YesNoAnswer)
            and answer.probability_yes >= self._rules.privacy_min_probability
        )
        return is_generic, CallCosts().add(result.cost_usd)

    async def _match_or_new(
        self, vocabulary_field: str, wording: str, candidates: Sequence[vocabulary.Candidate]
    ) -> tuple[_MatchDecision, CallCosts]:
        options = {
            f"{_CANDIDATE_OPTION_PREFIX}{index}": candidate.value
            for index, candidate in enumerate(candidates)
        }
        options[NEW_VALUE_OPTION] = self._prompts.vocabulary_new_option
        result = await self._classifier.decide(
            [self._prompts.vocabulary_input.format(field=vocabulary_field, wording=wording)],
            [
                ChoiceQuestion(
                    key=MATCH_QUESTION_KEY,
                    instructions=self._prompts.vocabulary_match_question.format(
                        field=vocabulary_field
                    ),
                    options=options,
                )
            ],
        )
        costs = CallCosts().add(result.cost_usd)
        answer = result.answers[MATCH_QUESTION_KEY]
        if not isinstance(answer, ChoiceAnswer):
            return _MatchDecision(candidate=None, probability=None, answered=False), costs
        probability = answer.probabilities.get(answer.choice)
        if answer.choice == NEW_VALUE_OPTION:
            return _MatchDecision(candidate=None, probability=probability, answered=True), costs
        index = int(answer.choice.removeprefix(_CANDIDATE_OPTION_PREFIX))
        return _MatchDecision(candidates[index], probability, answered=True), costs

    async def _record_match(self, form: str, decision: _MatchDecision) -> NormalizedValue:
        candidate = decision.candidate
        if candidate is None:
            raise ValueError("a match needs the value it was matched to")
        async with self._sessions() as session, session.begin():
            alias_id = await vocabulary.record_candidate_alias(
                session, candidate.vocabulary_id, form, decision.probability
            )
        return NormalizedValue(
            candidate.value,
            NormalizeOutcome.MATCHED,
            candidate.vocabulary_id,
            alias_id,
            decision.probability,
        )


def _phone_like(min_digits: int) -> re.Pattern[str]:
    # A digit, then at least `min_digits - 1` more, each after optional separators.
    return re.compile(rf"\+?\d(?:[\s().\-/]*\d){{{min_digits - 1},}}")


def _log_outcomes(open_values: Sequence[OpenValue], values: dict[str, NormalizedValue]) -> None:
    counts: dict[str, int] = {}
    for open_value in open_values:
        outcome = values[open_value.path].outcome
        counts[outcome] = counts.get(outcome, 0) + 1
    logger.info("open_values_normalized", extra={"outcomes": counts})
