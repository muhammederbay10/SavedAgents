"""A user's correction changes one item, never creates a synonym, and counts against the alias.

Items are indexed through the whole pipeline on fake models, so their
normalize step records the alias each value went through. These tests need
Postgres with PostGIS and pgvector (see `tests/database.py`).
"""

import logging
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.deletion import delete_user_data
from app.db.models import AliasStatus, SavedItem, Vocabulary, VocabularyAlias
from app.pipeline.vocabulary import store as vocabulary
from app.pipeline.vocabulary.corrections import (
    CorrectionError,
    ItemCorrection,
    UnknownItemError,
    UnknownValuePathError,
)
from app.pipeline.vocabulary.normalize import NormalizeOutcome
from app.pipeline.vocabulary.renormalize import AliasRejection
from app.types.registry import CONTENT_TYPES
from tests.pipeline.harness import RULES, Pipeline, add_item, add_user, load
from tests.pipeline.support import capture_event, place_extraction, structured_reply, transcript

WORDING = "Kore mutfağı"
CUISINE_PATH = "entities.0.cuisine"
REVIEW_MIN_CORRECTIONS = 2


@pytest.fixture
def pipeline(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> Pipeline:
    pipeline = Pipeline(sessions=session_factory, media_root=tmp_path / "media")
    pipeline.classifier.content_type = "place"
    pipeline.classifier.matches = {WORDING: "korean"}
    return pipeline


def corrections(pipeline: Pipeline) -> ItemCorrection:
    return ItemCorrection(
        pipeline.sessions,
        pipeline.normalizer(),
        CONTENT_TYPES,
        alias_min_probability=RULES.alias_min_probability,
        review_min_corrections=REVIEW_MIN_CORRECTIONS,
    )


async def add_korean(pipeline: Pipeline) -> None:
    vector = list((await pipeline.embedder.embed(["korean"])).vectors[0])
    async with pipeline.sessions() as session, session.begin():
        await vocabulary.add_value(session, "cuisine", "korean", vector)


async def index_place(pipeline: Pipeline, user_id: uuid.UUID, cuisine: str) -> uuid.UUID:
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[structured_reply(place_extraction(("Synthetic Place", cuisine)))],
    )
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    await pipeline.indexer().index(user_id, item_id)
    return item_id


async def the_alias(pipeline: Pipeline) -> VocabularyAlias:
    async with pipeline.sessions() as session:
        return (
            await session.execute(
                select(VocabularyAlias).where(VocabularyAlias.alias == "kore mutfağı")
            )
        ).scalar_one()


async def vocabulary_values(pipeline: Pipeline, field: str) -> set[str]:
    async with pipeline.sessions() as session:
        rows = await session.execute(select(Vocabulary.value).where(Vocabulary.field == field))
        return set(rows.scalars())


async def test_a_correction_changes_only_that_item_and_links_no_values(
    pipeline: Pipeline,
) -> None:
    await add_korean(pipeline)
    user_id = await add_user(pipeline.sessions)
    corrected = await index_place(pipeline, user_id, WORDING)
    untouched = await index_place(pipeline, user_id, WORDING)
    async with pipeline.sessions() as session:
        aliases_before = await session.scalar(select(func.count()).select_from(VocabularyAlias))

    result = await corrections(pipeline).correct(user_id, corrected, CUISINE_PATH, "japanese")

    assert (result.previous, result.value, result.outcome) == (
        "korean",
        "japanese",
        NormalizeOutcome.NEW,
    )
    item = await load(pipeline.sessions, corrected)
    assert item.fields["cuisine"] == ["japanese"]
    assert item.fields["entities"][0]["cuisine"] == "japanese"
    assert item.raw_values[CUISINE_PATH] == {
        "field": "cuisine",
        "wording": "japanese",
        "user_set": True,
    }
    assert (await load(pipeline.sessions, untouched)).fields["cuisine"] == ["korean"]
    async with pipeline.sessions() as session:
        aliases_after = await session.scalar(select(func.count()).select_from(VocabularyAlias))
        run = await vocabulary.normalize_run(session, corrected)
    # Only the user's wording became a value; "korean" and "japanese" are not linked.
    assert aliases_after == aliases_before
    assert run is not None
    assert run.output is not None
    assert run.output["values"][CUISINE_PATH]["outcome"] == NormalizeOutcome.NEW
    assert run.output["values"][CUISINE_PATH]["alias_id"] is None


async def test_a_corrected_candidate_alias_is_never_verified(pipeline: Pipeline) -> None:
    await add_korean(pipeline)
    users = [await add_user(pipeline.sessions) for _ in range(3)]
    first = await index_place(pipeline, users[0], WORDING)
    await corrections(pipeline).correct(users[0], first, CUISINE_PATH, "japanese")
    assert (await the_alias(pipeline)).correction_count == 1

    # Two more users match through it with confidence: enough to verify an
    # alias nobody corrected.
    await index_place(pipeline, users[1], WORDING)
    await index_place(pipeline, users[2], WORDING)

    alias = await the_alias(pipeline)
    assert alias.status == AliasStatus.CANDIDATE
    assert alias.correction_count == 1


async def test_repeated_corrections_flag_a_verified_alias_for_review(
    pipeline: Pipeline, caplog: pytest.LogCaptureFixture
) -> None:
    await add_korean(pipeline)
    first_user, second_user = await add_user(pipeline.sessions), await add_user(pipeline.sessions)
    first = await index_place(pipeline, first_user, WORDING)
    second = await index_place(pipeline, second_user, WORDING)
    alias = await the_alias(pipeline)
    assert alias.status == AliasStatus.VERIFIED

    with caplog.at_level(logging.WARNING):
        await corrections(pipeline).correct(first_user, first, CUISINE_PATH, "japanese")
        assert not [
            r for r in caplog.records if r.getMessage() == "vocabulary_alias_flagged_for_review"
        ]
        await corrections(pipeline).correct(second_user, second, CUISINE_PATH, "chinese")

    (flagged,) = [
        record
        for record in caplog.records
        if record.getMessage() == "vocabulary_alias_flagged_for_review"
    ]
    assert flagged.alias_id == str(alias.id)
    assert (await the_alias(pipeline)).status == AliasStatus.VERIFIED


@pytest.mark.parametrize("generic_probability", [0.1, 0.5])
async def test_wording_that_fails_the_privacy_gate_stays_private_on_the_item(
    pipeline: Pipeline, generic_probability: float
) -> None:
    private = "my ex's favourite place"
    pipeline.classifier.generic_probability = {private: generic_probability}
    user_id = await add_user(pipeline.sessions)
    item_id = await index_place(pipeline, user_id, "korean")

    result = await corrections(pipeline).correct(user_id, item_id, "category", private)

    assert (result.value, result.outcome) == (private, NormalizeOutcome.PRIVATE)
    assert (await load(pipeline.sessions, item_id)).category == private
    assert private.lower() not in await vocabulary_values(pipeline, "category")
    async with pipeline.sessions() as session:
        aliases = await session.scalar(
            select(func.count())
            .select_from(VocabularyAlias)
            .where(VocabularyAlias.alias == private.lower())
        )
    assert aliases == 0

    async with pipeline.sessions() as session, session.begin():
        await delete_user_data(session, user_id, pipeline.store)

    async with pipeline.sessions() as session:
        assert await session.get(SavedItem, item_id) is None
    assert private.lower() not in await vocabulary_values(pipeline, "category")


async def test_a_wording_with_a_handle_never_reaches_the_decision_model(
    pipeline: Pipeline,
) -> None:
    user_id = await add_user(pipeline.sessions)
    item_id = await index_place(pipeline, user_id, "korean")
    asked_before = len(pipeline.classifier.calls)

    result = await corrections(pipeline).correct(user_id, item_id, "tags.0", "@someone's spot")

    assert result.outcome == NormalizeOutcome.PRIVATE
    assert len(pipeline.classifier.calls) == asked_before
    assert (await load(pipeline.sessions, item_id)).tags == ["@someone's spot"]


async def test_rejecting_an_alias_keeps_the_users_correction(pipeline: Pipeline) -> None:
    await add_korean(pipeline)
    first_user, second_user = await add_user(pipeline.sessions), await add_user(pipeline.sessions)
    matched = await index_place(pipeline, first_user, WORDING)
    corrected = await index_place(pipeline, second_user, WORDING)
    await corrections(pipeline).correct(second_user, corrected, CUISINE_PATH, "turkish")
    alias = await the_alias(pipeline)

    rejection = AliasRejection(
        pipeline.sessions,
        pipeline.normalizer(),
        CONTENT_TYPES,
        alias_min_probability=RULES.alias_min_probability,
    )
    renormalized = await rejection.reject(alias.id)

    assert renormalized == [matched]
    assert (await load(pipeline.sessions, matched)).fields["cuisine"] == ["kore mutfağı"]
    assert (await load(pipeline.sessions, corrected)).fields["cuisine"] == ["turkish"]


async def test_another_users_item_cannot_be_corrected(pipeline: Pipeline) -> None:
    owner, stranger = await add_user(pipeline.sessions), await add_user(pipeline.sessions)
    item_id = await index_place(pipeline, owner, "korean")

    with pytest.raises(UnknownItemError):
        await corrections(pipeline).correct(stranger, item_id, "category", "bar")

    assert (await load(pipeline.sessions, item_id)).category == "restaurant"


async def test_an_unknown_path_names_the_items_open_values(pipeline: Pipeline) -> None:
    user_id = await add_user(pipeline.sessions)
    item_id = await index_place(pipeline, user_id, "korean")

    with pytest.raises(UnknownValuePathError, match=re.escape(CUISINE_PATH)):
        await corrections(pipeline).correct(user_id, item_id, "entities.0.name", "Other")


async def test_a_blank_correction_is_refused(pipeline: Pipeline) -> None:
    user_id = await add_user(pipeline.sessions)
    item_id = await index_place(pipeline, user_id, "korean")

    with pytest.raises(CorrectionError):
        await corrections(pipeline).correct(user_id, item_id, "category", "  ")
