"""Measure how often filter normalization picks the value the user meant (SAV-30).

Each line of `evals/normalization_cases.jsonl` is one synthetic case: one
user's saved values for one filter, what the user wrote, and the value they
meant, or `null` when the right answer is "none of these". The runner loads
each case's values into a throwaway database, runs
`FilterNormalizer.normalize()` on the wording, and prints a markdown report:
accuracy, wrong picks, misses, false matches, shortlist recall, how each case
was resolved, a threshold sweep, latency and cost.

    python -m scripts.run_normalization_evals           # offline fakes, no key
    python -m scripts.run_normalization_evals --live    # real models, costs money

Normalization itself is not changed. The runner wraps the decision model, so it
sees the options the model was shown and the probability of its pick. The
threshold sweep is computed from those probabilities, so it needs no second
model call.
"""

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import URL, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.search.conditions import WATCHABLE_OFFER_KINDS
from app.agent.search.normalization import FilterNormalizer, NormalizationMethod
from app.agent.search.prompts import RETRIEVAL_PROMPTS, RetrievalPrompts
from app.agent.search.query import (
    CATEGORY_SLOT,
    COLLECTION_SLOT,
    SearchQuery,
    SearchQueryError,
    Wording,
    plan_search,
)
from app.agent.search.saved_values import ValueScope
from app.config.base import get_settings
from app.config.models import get_model_settings
from app.config.retrieval import RetrievalSettings, get_retrieval_settings
from app.db.models import (
    AliasStatus,
    Base,
    Entity,
    EntityAvailability,
    ItemEntity,
    ItemStatus,
    SavedItem,
    User,
    Vocabulary,
    VocabularyAlias,
)
from app.logging_config import configure_logging
from app.models.base import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    Classifier,
    ContentPart,
    DecisionResult,
    Embedder,
    EmbeddingResult,
    Question,
)
from app.models.errors import ModelError
from app.models.factory import build_model_clients, open_http_client
from app.models.fakes import FakeClassifier, FakeEmbedder
from app.pipeline.vocabulary.store import canonical_form
from app.types.base import FilterKind, FilterSource
from app.types.registry import CONTENT_TYPES

logger = logging.getLogger(__name__)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parent.parent
CASES_PATH: Final = REPOSITORY_ROOT / "evals" / "normalization_cases.jsonl"
SWEEP_THRESHOLDS: Final = (0.5, 0.6, 0.7, 0.8, 0.9)
# A case's tag slot; collection and category use search's own slot names.
TAG_SLOT: Final = "tag"
EVAL_DATABASE_PREFIX: Final = "savely_eval_"
# The offline fakes' vectors carry no meaning, so a small size is enough.
OFFLINE_EMBEDDING_DIMENSION: Final = 8
OFFLINE_MODELS_LABEL: Final = "offline fakes: the decision model never answers (exact hits only)"
VOCABULARY_EMBEDDING_BATCH: Final = 100
MIGRATION_TIMEOUT_SECONDS: Final = 120.0
LATENCY_PERCENTILE: Final = 0.95
NOT_APPLICABLE: Final = "n/a"
EVAL_PLATFORM: Final = "eval"
EVAL_SOURCE: Final = "eval"
EVAL_MEDIA_TYPE: Final = "reel"
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
_RATES_NOTE: Final = (
    "Correct and model calls are shares of all cases; wrong pick and missed of the cases "
    "with an expected value; false match of the cases where none is right. Shortlist recall "
    "counts cases with an expected value that reached the model."
)


class CaseFileError(ValueError):
    """The case file cannot be read, or a case is not a valid search."""


class SweepMismatchError(RuntimeError):
    """The threshold sweep disagrees with what normalization did at the configured threshold.

    The sweep re-applies normalization's threshold rule to the recorded
    probabilities. If the two disagree, the rule changed and the sweep must
    follow it.
    """


class CaseKind(StrEnum):
    """What a case tests.

    Attributes:
        EXACT: Case and space differences or a verified alias; never calls the model.
        TYPO: A misspelling, a dropped accent or letter.
        SYNONYM: Another wording in the same language.
        TRANSLATION: The wording is in another language than the saved value.
        GRANULARITY: A broader or narrower value is saved next to, or instead of, the right one.
        NO_MATCH: None of the saved values is what the user meant.
        LARGE_LIST: More saved values than the model is shown.
    """

    EXACT = "exact"
    TYPO = "typo"
    SYNONYM = "synonym"
    TRANSLATION = "translation"
    GRANULARITY = "granularity"
    NO_MATCH = "no_match"
    LARGE_LIST = "large_list"


class Outcome(StrEnum):
    """How a case's result compares with what the user meant.

    Attributes:
        CORRECT: The expected value, or the wording kept when none was expected.
        WRONG_PICK: A different value was used.
        MISSED: A value was expected, and the wording was kept as written.
        FALSE_MATCH: None was expected, and a value was used.
    """

    CORRECT = "correct"
    WRONG_PICK = "wrong_pick"
    MISSED = "missed"
    FALSE_MATCH = "false_match"


class NormalizationCase(BaseModel):
    """One line of the case file.

    Attributes:
        id: Unique name of the case, e.g. `typo-03`.
        kind: What the case tests.
        language: The language of the wording, e.g. `tr`.
        slot: `collection`, `category`, `tag`, or a type's filter, e.g. `area`.
        type: The content type of a filter slot, e.g. `place`.
        country: The user's country, for filters that depend on it, e.g. `US`.
        wording: What the user wrote.
        expected: The saved value the user meant; `None` for "none of these".
        saved_values: The user's values for the slot: what normalization can pick from.
        verified_aliases: Verified aliases in the shared vocabulary, each mapped
            to the saved value it stands for.
        note: Why the case is there, for people reading the file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: CaseKind
    language: str = Field(min_length=2)
    slot: str = Field(min_length=1)
    type: str | None = None
    country: str | None = None
    wording: str = Field(min_length=1)
    expected: str | None
    saved_values: tuple[str, ...] = Field(min_length=1)
    verified_aliases: dict[str, str] = Field(default_factory=dict)
    note: str | None = None

    @model_validator(mode="after")
    def _values_are_consistent(self) -> Self:
        if len(set(self.saved_values)) != len(self.saved_values):
            raise ValueError("saved_values must not repeat a value")
        if self.expected is not None and self.expected not in self.saved_values:
            raise ValueError("expected must be one of saved_values, or null")
        if not set(self.verified_aliases.values()) <= set(self.saved_values):
            raise ValueError("every verified alias must stand for one of saved_values")
        return self


@dataclass(frozen=True)
class PreparedCase:
    """A case checked against the content types, with the wording search builds for it.

    Attributes:
        case: The case as written in the file.
        wording: The wording `plan_search` builds for the case's slot.
    """

    case: NormalizationCase
    wording: Wording


def load_cases(path: Path) -> tuple[PreparedCase, ...]:
    """Read and check every case in a case file.

    Args:
        path: A JSON Lines file, one case per line. Blank lines are skipped.

    Returns:
        The cases in file order, each with the wording search builds for it.

    Raises:
        CaseFileError: If the file can't be read, a line is not a valid case,
            two cases share an id, or a case is not a search `plan_search` accepts.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CaseFileError(f"cannot read the case file {path}: {error.strerror}") from error
    cases: list[PreparedCase] = []
    seen_ids: set[str] = set()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        case = _parse_case(line, number)
        if case.id in seen_ids:
            raise CaseFileError(f"line {number}: the case id {case.id} is used twice")
        seen_ids.add(case.id)
        cases.append(PreparedCase(case=case, wording=_wording_for(case)))
    return tuple(cases)


def _parse_case(line: str, number: int) -> NormalizationCase:
    try:
        return NormalizationCase.model_validate(json.loads(line))
    except json.JSONDecodeError as error:
        raise CaseFileError(f"line {number}: not valid JSON ({error.msg})") from error
    except ValidationError as error:
        raise CaseFileError(f"line {number}: not a valid case\n{error}") from error


def _wording_for(case: NormalizationCase) -> Wording:
    # The user id only names whose saves are searched; the wording does not depend on it.
    query = _query_for(case, uuid.uuid4())
    try:
        plan = plan_search(query, CONTENT_TYPES)
    except SearchQueryError as error:
        raise CaseFileError(f"case {case.id}: not a valid search: {error}") from error
    if len(plan.wordings) != 1:
        raise CaseFileError(f"case {case.id}: the slot {case.slot} is not normalized")
    return plan.wordings[0]


def _query_for(case: NormalizationCase, user_id: uuid.UUID) -> SearchQuery:
    if case.slot == COLLECTION_SLOT:
        return SearchQuery(user_id=user_id, collection=case.wording)
    if case.slot == CATEGORY_SLOT:
        return SearchQuery(user_id=user_id, category=case.wording)
    if case.slot == TAG_SLOT:
        return SearchQuery(user_id=user_id, tags=(case.wording,))
    return SearchQuery(
        user_id=user_id, country=case.country, type=case.type, filters={case.slot: case.wording}
    )


@dataclass
class DecisionCall:
    """One call to the decision model, as the runner saw it.

    Attributes:
        options: Each option's key and text, "none of these" included.
        answer: The model's answer; `None` if the call failed.
        cost_usd: What the provider charged; `None` if not reported.
        failed: Whether the call raised a model error.
    """

    options: Mapping[str, str]
    answer: Answer | None = None
    cost_usd: float | None = None
    failed: bool = False


class RecordingClassifier(Classifier):
    """Passes each call to a decision model and records what it was shown and answered."""

    def __init__(self, classifier: Classifier) -> None:
        """Wrap a decision model.

        Args:
            classifier: The model normalization would call.
        """
        self._classifier = classifier
        self._calls: list[DecisionCall] = []

    async def decide(
        self, state: Sequence[ContentPart], questions: Sequence[Question]
    ) -> DecisionResult:
        """Record the options, then ask the wrapped model.

        Args:
            state: What the question is about.
            questions: Normalization's one choice question.

        Returns:
            The wrapped model's result, unchanged.

        Raises:
            TypeError: If the call is not one choice question, as normalization sends.
            ModelError: If the wrapped model fails; the call is recorded as failed.
        """
        if len(questions) != 1 or not isinstance(questions[0], ChoiceQuestion):
            raise TypeError("normalization asks exactly one choice question per call")
        question = questions[0]
        call = DecisionCall(options=dict(question.options))
        self._calls.append(call)
        try:
            result = await self._classifier.decide(state, questions)
        except ModelError:
            call.failed = True
            raise
        call.answer = result.answers.get(question.key)
        call.cost_usd = result.cost_usd
        return result

    def take_calls(self) -> list[DecisionCall]:
        """Return the calls recorded since the last time, and forget them."""
        calls, self._calls = self._calls, []
        return calls


class RecordingEmbedder(Embedder):
    """Passes each call to an embedding model and counts calls and charges."""

    def __init__(self, embedder: Embedder) -> None:
        """Wrap an embedding model.

        Args:
            embedder: The model normalization would call.
        """
        self._embedder = embedder
        self._costs: list[float | None] = []

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """Ask the wrapped model, and record the call's charge.

        Args:
            texts: The texts to embed.

        Returns:
            The wrapped model's result, unchanged.

        Raises:
            ModelError: If the wrapped model fails; the call counts, without a charge.
        """
        try:
            result = await self._embedder.embed(texts)
        except ModelError:
            self._costs.append(None)
            raise
        self._costs.append(result.cost_usd)
        return result

    def take_costs(self) -> list[float | None]:
        """Return the charge of each call since the last time, and forget them."""
        costs, self._costs = self._costs, []
        return costs


@dataclass(frozen=True)
class ModelDecision:
    """What the decision model was shown for one case, and what it picked.

    Attributes:
        shortlist: The saved values among its options.
        picked: The saved value it chose; `None` for "none of these", an
            unusable answer or a failed call.
        probability: Its probability for that choice; 0 when it picked none.
        failed: Whether the call raised a model error.
    """

    shortlist: tuple[str, ...]
    picked: str | None
    probability: float
    failed: bool


@dataclass(frozen=True)
class CaseResult:
    """What normalization did with one case.

    Attributes:
        case: The case.
        method: How normalization found the value.
        value: The value the filter uses; the wording when nothing matched.
        decision: The decision model's call; `None` when it was not called.
        embedding_calls: How many embedding calls normalization made.
        latency_seconds: How long `normalize()` took, model calls included.
        cost_usd: What the provider charged for the case's model calls; `None`
            if no call reported a charge.
    """

    case: NormalizationCase
    method: NormalizationMethod
    value: str
    decision: ModelDecision | None
    embedding_calls: int
    latency_seconds: float
    cost_usd: float | None

    def used_value(self, threshold: float) -> str | None:
        """Return the value the filter would use at a threshold, by normalization's own rule.

        Args:
            threshold: The lowest probability of a pick that is used.

        Returns:
            The value; `None` when the wording would be kept as written.
        """
        if self.method in (NormalizationMethod.SAVED_VALUE, NormalizationMethod.VOCABULARY):
            return self.value
        decision = self.decision
        if decision is None or decision.picked is None or decision.probability < threshold:
            return None
        return decision.picked

    def outcome(self, threshold: float) -> Outcome:
        """Compare the value used at a threshold with the expected one.

        Values are compared in the vocabulary's canonical form, since a
        vocabulary hit returns the canonical value.

        Args:
            threshold: The lowest probability of a pick that is used.

        Returns:
            The outcome.
        """
        used = self.used_value(threshold)
        expected = self.case.expected
        if expected is None:
            return Outcome.CORRECT if used is None else Outcome.FALSE_MATCH
        if used is None:
            return Outcome.MISSED
        if canonical_form(used) == canonical_form(expected):
            return Outcome.CORRECT
        return Outcome.WRONG_PICK

    @property
    def shortlisted(self) -> bool | None:
        """Whether the expected value was among the options the model was shown.

        `None` when nothing was expected or the model was not called.
        """
        if self.case.expected is None or self.decision is None:
            return None
        return self.case.expected in self.decision.shortlist


@dataclass(frozen=True)
class EvalRun:
    """Every case's result, and the settings they ran with.

    Attributes:
        results: One result per case, in file order.
        min_probability: The configured threshold.
        max_options: The most options shown to the model.
        prompt_version: The version of the decision model's wording.
        setup_embedding_calls: Calls made to embed the vocabulary values.
        setup_cost_usd: Their charge; `None` if none was reported.
    """

    results: tuple[CaseResult, ...]
    min_probability: float
    max_options: int
    prompt_version: str
    setup_embedding_calls: int
    setup_cost_usd: float | None


@dataclass(frozen=True)
class _VocabularyVectors:
    vectors: Mapping[str, tuple[float, ...]]
    calls: int
    cost_usd: float | None


@dataclass
class _Seeded:
    items: list[SavedItem] = field(default_factory=list)
    entities: dict[int, Entity] = field(default_factory=dict)


async def run_cases(
    session: AsyncSession,
    cases: Sequence[PreparedCase],
    *,
    classifier: Classifier,
    embedder: Embedder,
    settings: RetrievalSettings,
    prompts: RetrievalPrompts = RETRIEVAL_PROMPTS,
) -> EvalRun:
    """Normalize every case's wording against its own saved values, one case at a time.

    Each case gets a new user. Its rows are written inside a savepoint that is
    rolled back after the case, so no case sees another's values.

    Args:
        session: A session on a migrated database, inside a transaction the
            caller rolls back.
        cases: The cases to run.
        classifier: The decision model.
        embedder: The embedding model; it also embeds the vocabulary values first.
        settings: The threshold and the most options, as search uses them.
        prompts: The wording sent to the decision model.

    Returns:
        Every case's result.

    Raises:
        ModelError: If the vocabulary values cannot be embedded.
        SweepMismatchError: If the sweep's rule disagrees with normalization.
    """
    recording_classifier = RecordingClassifier(classifier)
    recording_embedder = RecordingEmbedder(embedder)
    vocabulary = await _embed_vocabulary(recording_embedder, cases)
    normalizer = FilterNormalizer(
        classifier=recording_classifier,
        embedder=recording_embedder,
        prompts=prompts,
        settings=settings,
    )
    results: list[CaseResult] = []
    for prepared in cases:
        savepoint = await session.begin_nested()
        try:
            user_id = await _seed_case(session, prepared, vocabulary.vectors)
            scope = ValueScope(
                user_id=user_id, type=prepared.case.type, country=prepared.case.country
            )
            started = time.perf_counter()
            normalized = await normalizer.normalize(session, scope, [prepared.wording])
            latency = time.perf_counter() - started
        finally:
            await savepoint.rollback()
        result = _case_result(
            prepared,
            normalized[prepared.wording.slot].method,
            normalized[prepared.wording.slot].value,
            recording_classifier.take_calls(),
            recording_embedder.take_costs(),
            latency,
        )
        _require_sweep_matches(result, settings.filter_normalization_min_probability)
        results.append(result)
    return EvalRun(
        results=tuple(results),
        min_probability=settings.filter_normalization_min_probability,
        max_options=settings.filter_normalization_max_options,
        prompt_version=prompts.version,
        setup_embedding_calls=vocabulary.calls,
        setup_cost_usd=vocabulary.cost_usd,
    )


async def _embed_vocabulary(
    embedder: RecordingEmbedder, cases: Sequence[PreparedCase]
) -> _VocabularyVectors:
    """Embed the canonical form of every saved value of a vocabulary slot, as indexing does."""
    forms = sorted(
        {
            canonical_form(value)
            for prepared in cases
            if prepared.wording.vocabulary is not None
            for value in prepared.case.saved_values
        }
    )
    vectors: dict[str, tuple[float, ...]] = {}
    for start in range(0, len(forms), VOCABULARY_EMBEDDING_BATCH):
        batch = forms[start : start + VOCABULARY_EMBEDDING_BATCH]
        result = await embedder.embed(batch)
        vectors.update(zip(batch, result.vectors, strict=True))
    costs = embedder.take_costs()
    return _VocabularyVectors(vectors=vectors, calls=len(costs), cost_usd=_total_cost(costs))


async def _seed_case(
    session: AsyncSession, prepared: PreparedCase, vectors: Mapping[str, tuple[float, ...]]
) -> uuid.UUID:
    case, wording = prepared.case, prepared.wording
    user = User(display_name=f"eval {case.id}", country=case.country)
    session.add(user)
    await session.flush()
    seeded = _Seeded()
    for index, value in enumerate(case.saved_values):
        seeded.items.append(_saved_item(user.id, prepared, index, value))
        entity = _entity(prepared, index, value)
        if entity is not None:
            seeded.entities[index] = entity
    session.add_all([*seeded.items, *seeded.entities.values()])
    await session.flush()
    session.add_all(_links_and_availability(prepared, seeded))
    if wording.vocabulary is not None:
        await _add_vocabulary(session, wording.vocabulary, case, vectors)
    await session.flush()
    return user.id


def _saved_item(user_id: uuid.UUID, prepared: PreparedCase, index: int, value: str) -> SavedItem:
    """One saved item that holds `value` where search reads the slot's values."""
    case, wording = prepared.case, prepared.wording
    item = SavedItem(
        user_id=user_id,
        platform=EVAL_PLATFORM,
        platform_post_id=f"{case.id}-{index}",
        media_type=EVAL_MEDIA_TYPE,
        status=ItemStatus.DONE,
        type=case.type,
    )
    search_filter = wording.search_filter
    if wording.slot == COLLECTION_SLOT:
        item.collection = value
    elif wording.slot == CATEGORY_SLOT:
        item.category = value
    elif search_filter is None:
        item.tags = [value]
    elif (
        search_filter.kind is FilterKind.VALUE and search_filter.source is FilterSource.SAVED_VALUES
    ):
        item.fields = {search_filter.name: [value]}
    return item


def _entity(prepared: PreparedCase, index: int, value: str) -> Entity | None:
    """The linked entity that holds `value`, for slots search reads from entities.

    Normalization reads every address level of an area alike, so each area
    value is stored as a district.
    """
    search_filter = prepared.wording.search_filter
    if search_filter is None:
        return None
    is_area = search_filter.kind is FilterKind.AREA
    if not is_area and search_filter.source is not FilterSource.COUNTRY_AVAILABILITY:
        return None
    return Entity(
        type=prepared.case.type,
        source=EVAL_SOURCE,
        external_id=f"{prepared.case.id}-{index}",
        name=value,
        district=value if is_area else None,
    )


def _links_and_availability(prepared: PreparedCase, seeded: _Seeded) -> list[Base]:
    rows: list[Base] = []
    search_filter = prepared.wording.search_filter
    for index, entity in seeded.entities.items():
        rows.append(ItemEntity(saved_item_id=seeded.items[index].id, entity_id=entity.id))
        if search_filter is not None and search_filter.source is FilterSource.COUNTRY_AVAILABILITY:
            rows.append(
                EntityAvailability(
                    entity_id=entity.id,
                    country=prepared.case.country,
                    providers={WATCHABLE_OFFER_KINDS[0]: [entity.name]},
                )
            )
    return rows


async def _add_vocabulary(
    session: AsyncSession,
    vocabulary_field: str,
    case: NormalizationCase,
    vectors: Mapping[str, tuple[float, ...]],
) -> None:
    values: dict[str, Vocabulary] = {}
    for value in case.saved_values:
        form = canonical_form(value)
        values[form] = Vocabulary(field=vocabulary_field, value=form, embedding=list(vectors[form]))
    session.add_all(values.values())
    await session.flush()
    session.add_all(
        VocabularyAlias(
            vocabulary_id=values[canonical_form(target)].id,
            alias=canonical_form(alias),
            status=AliasStatus.VERIFIED,
        )
        for alias, target in case.verified_aliases.items()
    )


def _case_result(
    prepared: PreparedCase,
    method: NormalizationMethod,
    value: str,
    decision_calls: Sequence[DecisionCall],
    embedding_costs: Sequence[float | None],
    latency_seconds: float,
) -> CaseResult:
    decision = _model_decision(prepared.case, decision_calls[0]) if decision_calls else None
    costs = [*(call.cost_usd for call in decision_calls), *embedding_costs]
    return CaseResult(
        case=prepared.case,
        method=method,
        value=value,
        decision=decision,
        embedding_calls=len(embedding_costs),
        latency_seconds=latency_seconds,
        cost_usd=_total_cost(costs),
    )


def _model_decision(case: NormalizationCase, call: DecisionCall) -> ModelDecision:
    saved = set(case.saved_values)
    shortlist = tuple(option for option in call.options.values() if option in saved)
    answer = call.answer
    if not isinstance(answer, ChoiceAnswer) or call.options.get(answer.choice) not in saved:
        return ModelDecision(shortlist=shortlist, picked=None, probability=0.0, failed=call.failed)
    return ModelDecision(
        shortlist=shortlist,
        picked=call.options[answer.choice],
        probability=answer.probabilities.get(answer.choice, 0.0),
        failed=call.failed,
    )


def _require_sweep_matches(result: CaseResult, threshold: float) -> None:
    used_by_normalization = None if result.method is NormalizationMethod.UNCHANGED else result.value
    if result.used_value(threshold) != used_by_normalization:
        raise SweepMismatchError(
            f"case {result.case.id}: the sweep's rule disagrees with normalization's result"
        )


def _total_cost(costs: Iterable[float | None]) -> float | None:
    reported = [cost for cost in costs if cost is not None]
    return math.fsum(reported) if reported else None


@dataclass(frozen=True)
class Tally:
    """Counts of outcomes over a group of cases at one threshold.

    Attributes:
        cases: How many cases.
        expecting_value: Cases with an expected value.
        expecting_none: Cases where "none of these" is right.
        correct: Correct outcomes.
        wrong_pick: A different value was used.
        missed: A value was expected and the wording was kept.
        false_match: None was expected and a value was used.
        reached_model: Cases with an expected value that were sent to the model.
        shortlisted: Of those, cases whose expected value was among the options.
        model_calls: Cases that called the decision model.
    """

    cases: int
    expecting_value: int
    expecting_none: int
    correct: int
    wrong_pick: int
    missed: int
    false_match: int
    reached_model: int
    shortlisted: int
    model_calls: int


def tally(results: Sequence[CaseResult], threshold: float) -> Tally:
    """Count the outcomes of some cases at one threshold.

    Args:
        results: The cases' results.
        threshold: The lowest probability of a pick that is used.

    Returns:
        The counts.
    """
    outcomes = [result.outcome(threshold) for result in results]
    shortlisted = [result.shortlisted for result in results if result.shortlisted is not None]
    return Tally(
        cases=len(results),
        expecting_value=sum(result.case.expected is not None for result in results),
        expecting_none=sum(result.case.expected is None for result in results),
        correct=outcomes.count(Outcome.CORRECT),
        wrong_pick=outcomes.count(Outcome.WRONG_PICK),
        missed=outcomes.count(Outcome.MISSED),
        false_match=outcomes.count(Outcome.FALSE_MATCH),
        reached_model=len(shortlisted),
        shortlisted=sum(shortlisted),
        model_calls=sum(result.decision is not None for result in results),
    )


def render_report(run: EvalRun, models_label: str) -> str:
    """Write the run's results as markdown, ready to paste into a ticket.

    Args:
        run: The results.
        models_label: Which models answered, for the report's header.

    Returns:
        The report.
    """
    threshold = run.min_probability
    sections = [
        "## Filter normalization eval\n\n"
        f"{len(run.results)} cases · models: {models_label} · prompt version "
        f"`{run.prompt_version}` · threshold {threshold} · at most {run.max_options} options",
        _section("Overall", _summary_table([("All cases", list(run.results))], threshold)),
        _section("By case kind", _summary_table(_grouped(run.results, _kind_of), threshold)),
        _section("By language", _summary_table(_grouped(run.results, _language_of), threshold)),
        _RATES_NOTE,
        _section("How cases were resolved", _methods_table(run.results)),
        _section("Threshold sweep", _sweep_table(run.results, threshold)),
        _section("Latency and cost", _latency_table(run.results) + "\n\n" + _cost_line(run)),
        _section("Cases not correct", _failures_table(run.results, threshold)),
    ]
    return "\n\n".join(sections) + "\n"


def _section(title: str, body: str) -> str:
    return f"### {title}\n\n{body}"


def _kind_of(result: CaseResult) -> str:
    return result.case.kind.value


def _language_of(result: CaseResult) -> str:
    return result.case.language


def _grouped(
    results: Sequence[CaseResult], key: Callable[[CaseResult], str]
) -> list[tuple[str, list[CaseResult]]]:
    groups: dict[str, list[CaseResult]] = {}
    for result in results:
        groups.setdefault(key(result), []).append(result)
    return sorted(groups.items())


def _summary_table(groups: Sequence[tuple[str, Sequence[CaseResult]]], threshold: float) -> str:
    rows = [
        "| Group | Cases | Correct | Wrong pick | Missed | False match | Shortlist recall "
        "| Model called |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, results in groups:
        counts = tally(results, threshold)
        rows.append(
            f"| {_cell(name)} | {counts.cases} | {_share(counts.correct, counts.cases)} "
            f"| {_share(counts.wrong_pick, counts.expecting_value)} "
            f"| {_share(counts.missed, counts.expecting_value)} "
            f"| {_share(counts.false_match, counts.expecting_none)} "
            f"| {_ratio(counts.shortlisted, counts.reached_model)} "
            f"| {_share(counts.model_calls, counts.cases)} |"
        )
    return "\n".join(rows)


def _methods_table(results: Sequence[CaseResult]) -> str:
    rows = ["| Method | Cases |", "|---|---|"]
    for method in NormalizationMethod:
        count = sum(result.method is method for result in results)
        rows.append(f"| {method.value} | {_share(count, len(results))} |")
    failed = sum(result.decision is not None and result.decision.failed for result in results)
    return "\n".join(rows) + f"\n\nDecision calls that failed (wording kept): {failed}."


def _sweep_table(results: Sequence[CaseResult], configured: float) -> str:
    rows = [
        "| Threshold | Correct | Wrong pick | Missed | False match |",
        "|---|---|---|---|---|",
    ]
    for threshold in sorted({*SWEEP_THRESHOLDS, configured}):
        counts = tally(results, threshold)
        label = f"{threshold} (configured)" if threshold == configured else f"{threshold}"
        rows.append(
            f"| {label} | {_share(counts.correct, counts.cases)} "
            f"| {_share(counts.wrong_pick, counts.expecting_value)} "
            f"| {_share(counts.missed, counts.expecting_value)} "
            f"| {_share(counts.false_match, counts.expecting_none)} |"
        )
    return "\n".join(rows)


def _latency_table(results: Sequence[CaseResult]) -> str:
    rows = ["| Cases | Count | Median | 95th percentile | Slowest |", "|---|---|---|---|---|"]
    groups = [
        ("All", results),
        ("With a model call", [result for result in results if result.decision is not None]),
    ]
    for name, group in groups:
        latencies = sorted(result.latency_seconds for result in group)
        if not latencies:
            rows.append(f"| {name} | 0 | {NOT_APPLICABLE} | {NOT_APPLICABLE} | {NOT_APPLICABLE} |")
            continue
        rows.append(
            f"| {name} | {len(latencies)} | {_seconds(statistics.median(latencies))} "
            f"| {_seconds(_nearest_rank(latencies, LATENCY_PERCENTILE))} "
            f"| {_seconds(latencies[-1])} |"
        )
    return "\n".join(rows)


def _cost_line(run: EvalRun) -> str:
    decision_calls = sum(result.decision is not None for result in run.results)
    embedding_calls = sum(result.embedding_calls for result in run.results)
    cases_cost = _total_cost(result.cost_usd for result in run.results)
    return (
        f"Decision calls: {decision_calls} · embedding calls: {embedding_calls}, plus "
        f"{run.setup_embedding_calls} to embed the vocabulary · cost reported by the provider: "
        f"{_dollars(cases_cost)} for the cases, plus {_dollars(run.setup_cost_usd)} for the "
        "vocabulary."
    )


def _failures_table(results: Sequence[CaseResult], threshold: float) -> str:
    failures = [result for result in results if result.outcome(threshold) is not Outcome.CORRECT]
    if not failures:
        return "None."
    rows = [
        "| Case | Kind | Language | Outcome | Wrote | Expected | Used | Probability "
        "| Shortlisted |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for result in failures:
        case = result.case
        used = result.used_value(threshold)
        decision = result.decision
        probability = (
            NOT_APPLICABLE
            if decision is None or decision.picked is None
            else f"{decision.probability:.2f}"
        )
        shortlisted = {None: NOT_APPLICABLE, True: "yes", False: "no"}[result.shortlisted]
        rows.append(
            f"| {case.id} | {case.kind.value} | {case.language} "
            f"| {result.outcome(threshold).value} | {_cell(case.wording)} "
            f"| {_cell(case.expected or '(none)')} | {_cell(used or '(kept as written)')} "
            f"| {probability} | {shortlisted} |"
        )
    return "\n".join(rows)


def _share(count: int, total: int) -> str:
    if total == 0:
        return NOT_APPLICABLE
    return f"{count} ({count / total:.1%})"


def _ratio(count: int, total: int) -> str:
    if total == 0:
        return NOT_APPLICABLE
    return f"{count}/{total} ({count / total:.1%})"


def _seconds(value: float) -> str:
    return f"{value:.3f} s"


def _dollars(value: float | None) -> str:
    return "not reported" if value is None else f"${value:.4f}"


def _nearest_rank(sorted_values: Sequence[float], percentile: float) -> float:
    rank = max(math.ceil(percentile * len(sorted_values)), 1)
    return sorted_values[rank - 1]


def _cell(value: str) -> str:
    """Make a value safe inside a markdown table cell."""
    return " ".join(value.split()).replace("|", "\\|")


@asynccontextmanager
async def throwaway_database(server_url: URL, embedding_dimension: int) -> AsyncIterator[URL]:
    """Create a new migrated database for one run, and drop it afterwards.

    Args:
        server_url: Any database on the Postgres server to create it on. Only
            `CREATE DATABASE` and `DROP DATABASE` run there.
        embedding_dimension: The vector size of the embedding columns.

    Yields:
        The new database's URL.

    Raises:
        RuntimeError: If the migrations fail or time out.
    """
    name = f"{EVAL_DATABASE_PREFIX}{uuid.uuid4().hex}"
    await _execute_outside_transaction(server_url, f'CREATE DATABASE "{name}"')
    try:
        database_url = server_url.set(database=name)
        await _migrate(database_url, embedding_dimension)
        yield database_url
    finally:
        await _execute_outside_transaction(server_url, f'DROP DATABASE "{name}" WITH (FORCE)')


async def _execute_outside_transaction(url: URL, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _migrate(database_url: URL, embedding_dimension: int) -> None:
    """Run `alembic upgrade head` on the new database, in its own process."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "--config",
        str(REPOSITORY_ROOT / "alembic.ini"),
        "upgrade",
        "head",
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": database_url.render_as_string(hide_password=False),
            "EMBEDDING_DIMENSION": str(embedding_dimension),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), MIGRATION_TIMEOUT_SECONDS)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise RuntimeError("the migrations of the eval database timed out") from error
    if process.returncode != 0:
        raise RuntimeError(f"the migrations of the eval database failed:\n{stderr.decode()}")


async def run_in_throwaway_database(
    server_url: URL,
    cases: Sequence[PreparedCase],
    *,
    classifier: Classifier,
    embedder: Embedder,
    embedding_dimension: int,
    settings: RetrievalSettings,
) -> EvalRun:
    """Run every case in a new database that is dropped afterwards.

    Args:
        server_url: Any database on the Postgres server to create it on.
        cases: The cases.
        classifier: The decision model.
        embedder: The embedding model.
        embedding_dimension: The size of the embedder's vectors.
        settings: The threshold and the most options.

    Returns:
        Every case's result.
    """
    async with throwaway_database(server_url, embedding_dimension) as database_url:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                run = await run_cases(
                    session, cases, classifier=classifier, embedder=embedder, settings=settings
                )
                await session.rollback()
        finally:
            await engine.dispose()
    return run


async def _run(arguments: argparse.Namespace, cases: Sequence[PreparedCase]) -> str:
    settings = get_retrieval_settings()
    server_url = make_url(arguments.database_url or str(get_settings().database_url))
    if not arguments.live:
        run = await run_in_throwaway_database(
            server_url,
            cases,
            classifier=FakeClassifier(),
            embedder=FakeEmbedder(OFFLINE_EMBEDDING_DIMENSION),
            embedding_dimension=OFFLINE_EMBEDDING_DIMENSION,
            settings=settings,
        )
        return render_report(run, OFFLINE_MODELS_LABEL)
    model_settings = get_model_settings()
    logger.info(
        "normalization_eval_live_run",
        extra={
            "cases": len(cases),
            "most_decision_calls": len(cases),
            "decision_model": model_settings.decision_model,
            "embedding_model": model_settings.embedding_model,
        },
    )
    async with open_http_client(model_settings) as http_client:
        models = build_model_clients(model_settings, http_client)
        run = await run_in_throwaway_database(
            server_url,
            cases,
            classifier=models.classifier,
            embedder=models.embedder,
            embedding_dimension=model_settings.embedding_dimension,
            settings=settings,
        )
    return render_report(run, f"{model_settings.decision_model} + {model_settings.embedding_model}")


def _parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.run_normalization_evals",
        description="Measure filter normalization on synthetic cases and print a markdown report.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the real models through OpenRouter (needs OPENROUTER_API_KEY; costs money).",
    )
    parser.add_argument(
        "--cases", type=Path, default=CASES_PATH, help="The case file (JSON Lines)."
    )
    parser.add_argument(
        "--database-url",
        help=(
            "Any database on the Postgres server to create the throwaway database on. "
            "Defaults to DATABASE_URL."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """Run the eval and write the report to standard output.

    Args:
        argv: The command-line arguments, without the program name.

    Returns:
        The exit code: 0 when the report was written.
    """
    arguments = _parse_arguments(argv)
    configure_logging("INFO", stream=sys.stderr)
    # One log line per case would drown the run's own lines.
    logging.getLogger(FilterNormalizer.__module__).setLevel(logging.WARNING)
    try:
        cases = load_cases(arguments.cases)
        report = asyncio.run(_run(arguments, cases))
    except (
        CaseFileError,
        ValidationError,
        ModelError,
        SQLAlchemyError,
        OSError,
        RuntimeError,
    ) as error:
        logger.error("normalization_eval_failed", extra={"error": str(error)})
        return EXIT_FAILED
    sys.stdout.write(report)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
