"""The normalization eval runner (SAV-30): its case file, its counts and its report.

The five-case sample in `tests/fixtures/normalization_evals/` runs on fake
models whose picks are scripted, so every count below is known in advance:

| Case | Model pick | At 0.6 |
|---|---|---|
| sample-exact | not called | correct (exact hit) |
| sample-typo | Netflix, 0.95 | correct |
| sample-granularity | Beşiktaş Çarşı, 0.8 | wrong pick |
| sample-no-match | vietnamese, 0.7 | false match |
| sample-large-list | none; the expected value is not shortlisted | missed |
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.search.normalization import NormalizationMethod
from app.agent.search.prompts import RetrievalPrompts
from app.config.retrieval import RetrievalSettings
from app.models.base import (
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
from app.models.fakes import FakeClassifier, FakeEmbedder
from app.pipeline.vocabulary.store import canonical_form
from scripts.run_normalization_evals import (
    CASES_PATH,
    CaseFileError,
    CaseKind,
    EvalRun,
    Outcome,
    PreparedCase,
    load_cases,
    render_report,
    run_cases,
    tally,
)
from tests.database import TEST_EMBEDDING_DIMENSION

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "normalization_evals"
SAMPLE_CASES = SAMPLE_PATH / "sample_cases.jsonl"
TEST_PROMPTS = RetrievalPrompts(
    version="eval-test-1",
    normalization_question="Which saved {filter} does the user mean?",
    normalization_no_match_option="None of these.",
    normalization_input="{filter}|{wording}",
)
MIN_PROBABILITY = 0.6
MAX_OPTIONS = 20
DECISION_COST_USD = 0.002
EMBEDDING_COST_USD = 0.0001
PICKS = {
    "netflx": ("Netflix", 0.95),
    "besiktas": ("Beşiktaş Çarşı", 0.8),
    "thai": ("vietnamese", 0.7),
}
# The large-list case's wording and decoys share one axis; the expected value is far.
NEAR_AXIS = 0
FAR_AXIS = TEST_EMBEDDING_DIMENSION - 1
LARGE_LIST_WORDING = "sci-fi"
DECOY_PREFIX = "scifi-"


def retrieval_settings() -> RetrievalSettings:
    return RetrievalSettings(
        search_time_budget_seconds=10.0,
        search_max_results=20,
        search_min_results=1,
        filter_normalization_min_probability=MIN_PROBABILITY,
        filter_normalization_max_options=MAX_OPTIONS,
        availability_max_age_days=7.0,
    )


@dataclass
class ScriptedClassifier(Classifier):
    """Picks a scripted value among the options, by wording; anything else gets "none"."""

    picks: Mapping[str, tuple[str, float]]

    async def decide(
        self, state: Sequence[ContentPart], questions: Sequence[Question]
    ) -> DecisionResult:
        wording = str(state[0]).split("|", 1)[1]
        question = questions[0]
        assert isinstance(question, ChoiceQuestion)
        by_text = {text: key for key, text in question.options.items()}
        value, probability = self.picks.get(
            wording, (TEST_PROMPTS.normalization_no_match_option, 1.0)
        )
        choice = by_text[value]
        answer = ChoiceAnswer(choice=choice, probabilities={choice: probability}, confidence=None)
        return DecisionResult(
            answers={question.key: answer}, model="fake/decision", cost_usd=DECISION_COST_USD
        )


@dataclass
class AxisEmbedder(Embedder):
    """Puts the large-list wording and its decoys on one axis, and everything else far away."""

    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        self.calls.append(tuple(texts))
        vectors = tuple(_unit(_axis(text)) for text in texts)
        return EmbeddingResult(vectors=vectors, model="fake/embedding", cost_usd=EMBEDDING_COST_USD)


def _axis(text: str) -> int:
    near = text == LARGE_LIST_WORDING or text.startswith(DECOY_PREFIX)
    return NEAR_AXIS if near else FAR_AXIS


def _unit(axis: int) -> tuple[float, ...]:
    return tuple(1.0 if index == axis else 0.0 for index in range(TEST_EMBEDDING_DIMENSION))


async def run_sample(db_session: AsyncSession) -> EvalRun:
    return await run_cases(
        db_session,
        load_cases(SAMPLE_CASES),
        classifier=ScriptedClassifier(PICKS),
        embedder=AxisEmbedder(),
        settings=retrieval_settings(),
        prompts=TEST_PROMPTS,
    )


def _by_id(run: EvalRun) -> dict[str, Outcome]:
    return {result.case.id: result.outcome(MIN_PROBABILITY) for result in run.results}


async def test_five_case_sample_counts_wrong_picks_misses_false_matches_and_recall(
    db_session: AsyncSession,
) -> None:
    run = await run_sample(db_session)

    assert _by_id(run) == {
        "sample-exact": Outcome.CORRECT,
        "sample-typo": Outcome.CORRECT,
        "sample-granularity": Outcome.WRONG_PICK,
        "sample-no-match": Outcome.FALSE_MATCH,
        "sample-large-list": Outcome.MISSED,
    }
    counts = tally(run.results, MIN_PROBABILITY)
    assert (counts.correct, counts.wrong_pick, counts.missed, counts.false_match) == (2, 1, 1, 1)
    assert (counts.shortlisted, counts.reached_model) == (2, 3)
    assert counts.model_calls == 4


async def test_sample_cases_are_resolved_the_way_normalization_resolved_them(
    db_session: AsyncSession,
) -> None:
    run = await run_sample(db_session)

    methods = {result.case.id: result.method for result in run.results}
    assert methods == {
        "sample-exact": NormalizationMethod.SAVED_VALUE,
        "sample-typo": NormalizationMethod.DECISION,
        "sample-granularity": NormalizationMethod.DECISION,
        "sample-no-match": NormalizationMethod.DECISION,
        "sample-large-list": NormalizationMethod.UNCHANGED,
    }
    large_list = run.results[-1]
    assert large_list.decision is not None
    assert len(large_list.decision.shortlist) == MAX_OPTIONS
    assert "science fiction" not in large_list.decision.shortlist


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (0.5, (2, 1, 1, 1)),
        (0.7, (2, 1, 1, 1)),
        (0.8, (3, 1, 1, 0)),
        (0.9, (3, 0, 2, 0)),
    ],
)
async def test_threshold_sweep_reapplies_the_threshold_to_the_recorded_probabilities(
    db_session: AsyncSession, threshold: float, expected: tuple[int, int, int, int]
) -> None:
    run = await run_sample(db_session)

    counts = tally(run.results, threshold)
    assert (counts.correct, counts.wrong_pick, counts.missed, counts.false_match) == expected


async def test_report_shows_every_metric_for_the_sample(db_session: AsyncSession) -> None:
    run = await run_sample(db_session)

    report = render_report(run, "scripted fakes")

    assert (
        "| All cases | 5 | 2 (40.0%) | 1 (25.0%) | 1 (25.0%) | 1 (100.0%) | 2/3 (66.7%) "
        "| 4 (80.0%) |" in report
    )
    assert "| large_list | 1 | 0 (0.0%) | 0 (0.0%) | 1 (100.0%) | n/a | 0/1 (0.0%) |" in report
    assert "| tr | 1 | 0 (0.0%) | 1 (100.0%) | 0 (0.0%) | n/a | 1/1 (100.0%) |" in report
    assert "| saved_value | 1 (20.0%) |" in report
    assert "| decision | 3 (60.0%) |" in report
    assert "| 0.6 (configured) | 2 (40.0%) | 1 (25.0%) | 1 (25.0%) | 1 (100.0%) |" in report
    assert "| 0.9 | 3 (60.0%) | 0 (0.0%) | 2 (50.0%) | 0 (0.0%) |" in report
    assert "| With a model call | 4 |" in report
    # Four decision calls, two wording embeddings, and one call for the vocabulary.
    assert "Decision calls: 4 · embedding calls: 2, plus 1 to embed the vocabulary" in report
    assert "$0.0082 for the cases, plus $0.0001 for the vocabulary" in report
    assert (
        "| sample-granularity | granularity | tr | wrong_pick | besiktas | Beşiktaş "
        "| Beşiktaş Çarşı | 0.80 | yes |" in report
    )
    assert "| sample-large-list | large_list | en | missed |" in report
    assert "sample-typo |" not in report


async def test_a_case_never_sees_the_vocabulary_another_case_added(
    db_session: AsyncSession,
) -> None:
    no_match = load_cases(SAMPLE_CASES)[3]
    # The vocabulary is shared by all users, so a value left over from the first case
    # would be an exact hit for the second.
    later = PreparedCase(
        case=no_match.case.model_copy(
            update={"id": "later", "wording": "vietnamese", "saved_values": ("thai",)}
        ),
        wording=replace(no_match.wording, wording="vietnamese"),
    )

    run = await run_cases(
        db_session,
        [no_match, later],
        classifier=FakeClassifier(),
        embedder=FakeEmbedder(TEST_EMBEDDING_DIMENSION),
        settings=retrieval_settings(),
    )

    assert run.results[1].method is NormalizationMethod.UNCHANGED
    assert run.results[1].outcome(MIN_PROBABILITY) is Outcome.CORRECT


async def test_a_failed_decision_call_keeps_the_wording_and_counts_as_missed(
    db_session: AsyncSession,
) -> None:
    error = ModelError("decision failed", operation="decide", model="fake/decision")

    run = await run_cases(
        db_session,
        load_cases(SAMPLE_CASES)[1:2],
        classifier=FakeClassifier(error=error),
        embedder=FakeEmbedder(TEST_EMBEDDING_DIMENSION),
        settings=retrieval_settings(),
    )

    (result,) = run.results
    assert result.method is NormalizationMethod.UNCHANGED
    assert result.decision is not None and result.decision.failed
    assert result.shortlisted is True
    assert result.outcome(MIN_PROBABILITY) is Outcome.MISSED
    assert "Decision calls that failed (wording kept): 1." in render_report(run, "fakes")


def test_case_file_covers_every_kind_with_enough_no_match_cases_and_languages() -> None:
    cases = [prepared.case for prepared in load_cases(CASES_PATH)]

    assert 90 <= len(cases) <= 110
    assert {case.kind for case in cases} == set(CaseKind)
    assert sum(case.expected is None for case in cases) >= 15
    assert sum(case.kind is CaseKind.NO_MATCH for case in cases) >= 15
    other_languages = {case.language for case in cases} - {"en"}
    assert {"tr", "ar"} <= other_languages
    assert len(other_languages) >= 3
    large_lists = [case for case in cases if case.kind is CaseKind.LARGE_LIST]
    assert large_lists
    assert all(len(case.saved_values) > MAX_OPTIONS for case in large_lists)


def test_case_file_uses_every_kind_of_slot() -> None:
    slots = {prepared.wording.slot for prepared in load_cases(CASES_PATH)}

    assert {"collection", "category", "tags.0", "filters.area", "filters.streaming"} <= slots
    assert {"filters.cuisine", "filters.genre", "filters.subject"} <= slots


def test_only_exact_cases_are_exact_hits() -> None:
    for prepared in load_cases(CASES_PATH):
        case = prepared.case
        exact_forms = {canonical_form(value) for value in case.saved_values}
        exact_forms |= {canonical_form(alias) for alias in case.verified_aliases}
        is_exact_hit = canonical_form(case.wording) in exact_forms
        assert is_exact_hit == (case.kind is CaseKind.EXACT), case.id


def test_a_case_whose_expected_value_is_not_saved_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id": "a", "kind": "typo", "language": "en", "slot": "collection", '
        '"wording": "fod", "expected": "Food", "saved_values": ["Movies"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(CaseFileError, match="line 1: not a valid case"):
        load_cases(path)


def test_a_filter_case_without_a_content_type_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id": "a", "kind": "typo", "language": "en", "slot": "area", '
        '"wording": "bruklin", "expected": "Brooklyn", "saved_values": ["Brooklyn"]}\n',
        encoding="utf-8",
    )

    with pytest.raises(CaseFileError, match="case a: not a valid search"):
        load_cases(path)


def test_two_cases_with_one_id_are_refused(tmp_path: Path) -> None:
    line = (
        '{"id": "a", "kind": "exact", "language": "en", "slot": "collection", '
        '"wording": "food", "expected": "Food", "saved_values": ["Food"]}\n'
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(line + "\n" + line, encoding="utf-8")

    with pytest.raises(CaseFileError, match="line 3: the case id a is used twice"):
        load_cases(path)
