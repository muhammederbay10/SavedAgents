"""Normalizing against the shared vocabulary: exact hits, privacy gate, match-or-new, verification.

These tests need Postgres with PostGIS and pgvector (see `tests/database.py`).
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AliasStatus, EnrichmentRun, SavedItem, Vocabulary, VocabularyAlias
from app.pipeline.recovery.runs import RunStatus, Step
from app.pipeline.vocabulary import store as vocabulary
from app.pipeline.vocabulary.normalize import PROVENANCE_VALUES, NormalizeOutcome
from app.pipeline.vocabulary.open_values import OpenValue
from tests.pipeline.harness import RULES, Pipeline, add_user
from tests.pipeline.support import TEST_PROMPTS

CUISINE = "cuisine"


@pytest.fixture
def pipeline(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> Pipeline:
    return Pipeline(sessions=session_factory, media_root=tmp_path / "media")


def cuisine(wording: str, path: str = "entities.0.cuisine") -> OpenValue:
    return OpenValue(path, CUISINE, wording, nested=True)


async def add_value(pipeline: Pipeline, value: str) -> uuid.UUID:
    vector = list((await pipeline.embedder.embed([value])).vectors[0])
    async with pipeline.sessions() as session, session.begin():
        return await vocabulary.add_value(session, CUISINE, value, vector)


async def add_alias(
    pipeline: Pipeline, vocabulary_id: uuid.UUID, alias: str, status: AliasStatus
) -> uuid.UUID:
    async with pipeline.sessions() as session, session.begin():
        row = VocabularyAlias(vocabulary_id=vocabulary_id, alias=alias, status=status)
        session.add(row)
        await session.flush()
        return row.id


async def count(pipeline: Pipeline, model: type[Vocabulary] | type[VocabularyAlias]) -> int:
    async with pipeline.sessions() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def alias_status(pipeline: Pipeline, alias_id: uuid.UUID) -> str:
    async with pipeline.sessions() as session:
        alias = await session.get(VocabularyAlias, alias_id)
        assert alias is not None
        return alias.status


def reset_model_calls(pipeline: Pipeline) -> None:
    pipeline.classifier.calls.clear()
    pipeline.embedder.calls.clear()


async def test_an_exact_canonical_value_is_reused_without_any_model_call(
    pipeline: Pipeline,
) -> None:
    value_id = await add_value(pipeline, "korean")
    reset_model_calls(pipeline)

    result = await pipeline.normalizer().normalize([cuisine("  Korean ")])

    normalized = result.values["entities.0.cuisine"]
    assert (normalized.value, normalized.outcome) == ("korean", NormalizeOutcome.EXACT_VALUE)
    assert normalized.vocabulary_id == value_id
    assert pipeline.classifier.calls == [] and pipeline.embedder.calls == []


async def test_a_verified_alias_is_an_exact_hit_without_any_model_call(pipeline: Pipeline) -> None:
    value_id = await add_value(pipeline, "korean")
    alias_id = await add_alias(pipeline, value_id, "kore mutfağı", AliasStatus.VERIFIED)
    reset_model_calls(pipeline)

    result = await pipeline.normalizer().normalize([cuisine("Kore Mutfağı")])

    normalized = result.values["entities.0.cuisine"]
    assert normalized.value == "korean"
    assert normalized.outcome is NormalizeOutcome.EXACT_ALIAS
    assert normalized.alias_id == alias_id
    assert pipeline.classifier.calls == [] and pipeline.embedder.calls == []


async def test_a_candidate_alias_is_no_shortcut(pipeline: Pipeline) -> None:
    value_id = await add_value(pipeline, "korean")
    alias_id = await add_alias(pipeline, value_id, "kore mutfağı", AliasStatus.CANDIDATE)
    pipeline.classifier.matches = {"Kore mutfağı": "korean"}
    reset_model_calls(pipeline)

    result = await pipeline.normalizer().normalize([cuisine("Kore mutfağı")])

    normalized = result.values["entities.0.cuisine"]
    assert normalized.outcome is NormalizeOutcome.MATCHED
    assert normalized.alias_id == alias_id
    assert pipeline.classifier.asked_keys() == ["generic", "same_value"]
    assert await alias_status(pipeline, alias_id) == AliasStatus.CANDIDATE


async def test_a_rejected_alias_is_no_shortcut_and_its_value_is_not_offered_again(
    pipeline: Pipeline,
) -> None:
    value_id = await add_value(pipeline, "korean")
    await add_alias(pipeline, value_id, "kore mutfağı", AliasStatus.REJECTED)
    pipeline.classifier.matches = {"Kore mutfağı": "korean"}
    reset_model_calls(pipeline)

    result = await pipeline.normalizer().normalize([cuisine("Kore mutfağı")])

    normalized = result.values["entities.0.cuisine"]
    assert normalized.outcome is NormalizeOutcome.NEW
    assert normalized.value == "kore mutfağı"
    assert pipeline.classifier.asked_keys() == ["generic"]


async def test_a_nearby_value_is_only_a_candidate_until_the_decision_model_matches_it(
    pipeline: Pipeline,
) -> None:
    korean_id = await add_value(pipeline, "korean")
    pipeline.classifier.matches = {"Korean Food": "korean"}

    result = await pipeline.normalizer().normalize(
        [cuisine("Korean Food"), cuisine("Japanese", "x")]
    )

    matched, new = result.values["entities.0.cuisine"], result.values["x"]
    assert (matched.value, matched.outcome) == ("korean", NormalizeOutcome.MATCHED)
    assert matched.vocabulary_id == korean_id
    assert matched.probability == 0.95
    assert (new.value, new.outcome) == ("japanese", NormalizeOutcome.NEW)
    match_questions = [
        question
        for _, questions in pipeline.classifier.calls
        for question in questions
        if question.key == "same_value"
    ]
    assert len(match_questions) == 2
    assert match_questions[0].instructions == "test: same cuisine?"
    assert "korean" in match_questions[1].options.values()  # type: ignore[union-attr]
    async with pipeline.sessions() as session:
        aliases = (await session.execute(select(VocabularyAlias))).scalars().all()
    assert [(alias.alias, alias.status, alias.probability) for alias in aliases] == [
        ("korean food", AliasStatus.CANDIDATE, 0.95)
    ]


@pytest.mark.parametrize(
    "wording",
    ["@grandma.cooks", "ask someone@example.test", "call 0532 000 00 00", "x" * 41],
    ids=["handle", "email", "phone", "too-long"],
)
async def test_wording_that_fails_the_code_checks_stays_on_the_item_only(
    pipeline: Pipeline, wording: str
) -> None:
    result = await pipeline.normalizer().normalize([cuisine(wording)])

    normalized = result.values["entities.0.cuisine"]
    assert (normalized.value, normalized.outcome) == (wording, NormalizeOutcome.PRIVATE)
    assert pipeline.classifier.calls == []
    assert await count(pipeline, Vocabulary) == 0


async def test_uncertain_wording_never_becomes_a_shared_value(pipeline: Pipeline) -> None:
    pipeline.classifier.generic_probability = {"Ayşe teyzenin köftesi": 0.6}

    result = await pipeline.normalizer().normalize([cuisine("Ayşe teyzenin köftesi")])

    normalized = result.values["entities.0.cuisine"]
    assert normalized.outcome is NormalizeOutcome.PRIVATE
    assert normalized.value == "Ayşe teyzenin köftesi"
    assert await count(pipeline, Vocabulary) == 0
    assert pipeline.embedder.calls == []


async def test_uncertain_wording_never_becomes_a_shared_alias(pipeline: Pipeline) -> None:
    await add_value(pipeline, "meatballs")
    pipeline.classifier.generic_probability = {"my ex's favourite köfte": 0.89}
    pipeline.classifier.matches = {"my ex's favourite köfte": "meatballs"}
    reset_model_calls(pipeline)

    result = await pipeline.normalizer().normalize([cuisine("my ex's favourite köfte")])

    assert result.values["entities.0.cuisine"].outcome is NormalizeOutcome.PRIVATE
    assert await count(pipeline, VocabularyAlias) == 0
    assert pipeline.classifier.asked_keys() == ["generic"]


async def test_the_same_wording_twice_in_one_item_is_decided_once(pipeline: Pipeline) -> None:
    result = await pipeline.normalizer().normalize([cuisine("Ramen", "a"), cuisine("ramen ", "b")])

    assert result.values["a"] == result.values["b"]
    assert pipeline.classifier.asked_keys() == ["generic"]


async def add_matched_item(
    pipeline: Pipeline, user_id: uuid.UUID, alias_id: uuid.UUID, probability: float
) -> None:
    async with pipeline.sessions() as session, session.begin():
        item = SavedItem(
            user_id=user_id, platform="instagram", post_url=f"https://x.test/{uuid.uuid4()}"
        )
        session.add(item)
        await session.flush()
        session.add(
            EnrichmentRun(
                saved_item_id=item.id,
                step=Step.NORMALIZE,
                status=RunStatus.DONE,
                output={
                    PROVENANCE_VALUES: {
                        "entities.0.cuisine": {
                            "field": CUISINE,
                            "outcome": "matched",
                            "alias_id": str(alias_id),
                            "probability": probability,
                        }
                    }
                },
            )
        )


async def test_an_alias_is_verified_after_confident_matches_from_enough_different_users(
    pipeline: Pipeline,
) -> None:
    value_id = await add_value(pipeline, "korean")
    alias_id = await add_alias(pipeline, value_id, "korean food", AliasStatus.CANDIDATE)
    first_user, second_user = await add_user(pipeline.sessions), await add_user(pipeline.sessions)
    await add_matched_item(pipeline, first_user, alias_id, 0.95)
    await add_matched_item(pipeline, first_user, alias_id, 0.97)
    await add_matched_item(pipeline, second_user, alias_id, RULES.alias_min_probability - 0.1)

    await pipeline.normalizer().verify_aliases({alias_id})
    assert await alias_status(pipeline, alias_id) == AliasStatus.CANDIDATE

    await add_matched_item(pipeline, second_user, alias_id, 0.93)
    await pipeline.normalizer().verify_aliases({alias_id})
    assert await alias_status(pipeline, alias_id) == AliasStatus.VERIFIED


def test_the_vocabulary_question_carries_the_field_and_wording_as_state() -> None:
    assert TEST_PROMPTS.vocabulary_input.format(field=CUISINE, wording="x") == "cuisine\nx"
