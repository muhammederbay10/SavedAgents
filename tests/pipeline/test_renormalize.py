"""Rejecting an alias normalizes again only the items that matched through it.

These tests need Postgres with PostGIS and pgvector (see `tests/database.py`).
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AliasStatus, SavedItem, Vocabulary, VocabularyAlias
from app.pipeline import vocabulary
from app.pipeline.renormalize import AliasRejection, UnknownAliasError
from app.types.registry import CONTENT_TYPES
from tests.pipeline.harness import RULES, Pipeline, add_item, add_user, load
from tests.pipeline.support import capture_event, place_extraction, structured_reply, transcript

WORDING = "Kore mutfağı"


@pytest.fixture
def pipeline(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> Pipeline:
    pipeline = Pipeline(sessions=session_factory, media_root=tmp_path / "media")
    pipeline.classifier.content_type = "place"
    pipeline.classifier.matches = {WORDING: "korean"}
    return pipeline


async def index_place(pipeline: Pipeline, user_id: uuid.UUID, cuisine: str) -> uuid.UUID:
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[structured_reply(place_extraction(("Synthetic Place", cuisine)))],
    )
    item_id = await add_item(pipeline.sessions, pipeline, capture_event(user_id))
    await pipeline.indexer().index(user_id, item_id, last_try=False)
    return item_id


async def counts(pipeline: Pipeline) -> dict[str, int]:
    async with pipeline.sessions() as session:
        rows = await session.execute(
            select(Vocabulary.value, Vocabulary.item_count).where(Vocabulary.field == "cuisine")
        )
        return {row.value: row.item_count for row in rows}


async def the_alias(pipeline: Pipeline) -> VocabularyAlias:
    async with pipeline.sessions() as session:
        return (
            await session.execute(
                select(VocabularyAlias).where(VocabularyAlias.alias == "kore mutfağı")
            )
        ).scalar_one()


async def correct_by_hand(pipeline: Pipeline, item_id: uuid.UUID, value: str) -> None:
    """What a user's correction (SAV-17) leaves on an item: the value, marked as set by hand."""
    async with pipeline.sessions() as session, session.begin():
        item = await session.get(SavedItem, item_id)
        assert item is not None
        entities = [{**entity, "cuisine": value} for entity in item.fields["entities"]]
        item.fields = {**item.fields, "entities": entities, "cuisine": [value]}
        item.raw_values = {
            **item.raw_values,
            "entities.0.cuisine": {"field": "cuisine", "wording": value, "user_set": True},
        }


async def test_rejecting_an_alias_renormalizes_only_its_items_and_keeps_manual_values(
    pipeline: Pipeline,
) -> None:
    vector = list((await pipeline.embedder.embed(["korean"])).vectors[0])
    async with pipeline.sessions() as session, session.begin():
        await vocabulary.add_value(session, "cuisine", "korean", vector)
    first_user, second_user = await add_user(pipeline.sessions), await add_user(pipeline.sessions)
    matched = await index_place(pipeline, first_user, WORDING)
    corrected = await index_place(pipeline, second_user, WORDING)
    unrelated = await index_place(pipeline, first_user, "Korean")
    await correct_by_hand(pipeline, corrected, "turkish")
    alias = await the_alias(pipeline)
    # Two different users matched through it with confidence, so it was verified.
    assert alias.status == AliasStatus.VERIFIED
    assert (await counts(pipeline))["korean"] == 3

    rejection = AliasRejection(
        pipeline.sessions,
        pipeline.normalizer(),
        CONTENT_TYPES,
        alias_min_probability=RULES.alias_min_probability,
    )
    linked = await rejection.reject(alias.id)

    assert set(linked) == {matched, corrected}
    assert (await the_alias(pipeline)).status == AliasStatus.REJECTED
    renormalized = await load(pipeline.sessions, matched)
    assert renormalized.fields["cuisine"] == ["kore mutfağı"]
    assert renormalized.fields["entities"][0]["cuisine"] == "kore mutfağı"
    assert renormalized.raw_values["entities.0.cuisine"]["wording"] == WORDING
    assert (await load(pipeline.sessions, corrected)).fields["cuisine"] == ["turkish"]
    assert (await load(pipeline.sessions, unrelated)).fields["cuisine"] == ["korean"]
    assert (await counts(pipeline))["korean"] == 2
    assert (await counts(pipeline))["kore mutfağı"] == 1


async def test_a_rejected_alias_is_never_matched_again(pipeline: Pipeline) -> None:
    vector = list((await pipeline.embedder.embed(["korean"])).vectors[0])
    async with pipeline.sessions() as session, session.begin():
        await vocabulary.add_value(session, "cuisine", "korean", vector)
    user_id = await add_user(pipeline.sessions)
    await index_place(pipeline, user_id, WORDING)
    alias = await the_alias(pipeline)
    rejection = AliasRejection(
        pipeline.sessions, pipeline.normalizer(), CONTENT_TYPES, alias_min_probability=0.9
    )
    await rejection.reject(alias.id)

    later = await index_place(pipeline, user_id, WORDING)

    assert (await load(pipeline.sessions, later)).fields["cuisine"] == ["kore mutfağı"]


async def test_rejecting_an_unknown_alias_fails(pipeline: Pipeline) -> None:
    rejection = AliasRejection(
        pipeline.sessions, pipeline.normalizer(), CONTENT_TYPES, alias_min_probability=0.9
    )

    with pytest.raises(UnknownAliasError):
        await rejection.reject(uuid.uuid4())


async def test_a_correction_made_while_the_models_work_is_kept(pipeline: Pipeline) -> None:
    vector = list((await pipeline.embedder.embed(["korean"])).vectors[0])
    async with pipeline.sessions() as session, session.begin():
        await vocabulary.add_value(session, "cuisine", "korean", vector)
    user_id, other_user_id = await add_user(pipeline.sessions), await add_user(pipeline.sessions)
    corrected_meanwhile = await index_place(pipeline, user_id, WORDING)
    renormalized = await index_place(pipeline, other_user_id, WORDING)
    alias = await the_alias(pipeline)

    async def correct_during_model_work() -> None:
        await correct_by_hand(pipeline, corrected_meanwhile, "turkish")

    # The privacy gate is the first model call of the re-normalization; the
    # user corrects the item while it waits for the answer.
    pipeline.classifier.before_decide = correct_during_model_work
    rejection = AliasRejection(
        pipeline.sessions, pipeline.normalizer(), CONTENT_TYPES, alias_min_probability=0.9
    )
    await rejection.reject(alias.id)

    kept = await load(pipeline.sessions, corrected_meanwhile)
    assert kept.fields["cuisine"] == ["turkish"]
    assert kept.fields["entities"][0]["cuisine"] == "turkish"
    assert kept.raw_values["entities.0.cuisine"] == {
        "field": "cuisine",
        "wording": "turkish",
        "user_set": True,
    }
    assert (await load(pipeline.sessions, renormalized)).fields["cuisine"] == ["kore mutfağı"]
