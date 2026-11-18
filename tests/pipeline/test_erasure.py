"""Delete my data also removes the media and the step outputs the pipeline kept.

These tests need Postgres with PostGIS and pgvector (see `tests/database.py`).
"""

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.deletion import delete_user_data
from app.db.models import EnrichmentRun, Vocabulary
from tests.pipeline.harness import Pipeline, add_item, add_user
from tests.pipeline.support import capture_event, open_extraction, structured_reply, transcript


async def test_deleting_a_user_removes_their_media_step_outputs_and_counts(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    pipeline = Pipeline(sessions=session_factory, media_root=tmp_path / "media")
    pipeline.script(
        transcripts=[transcript("synthetic speech")],
        extractions=[structured_reply(open_extraction("Car Review", ["Sports Car"]))],
    )
    leaving, staying = await add_user(session_factory), await add_user(session_factory)
    item_id = await add_item(session_factory, pipeline, capture_event(leaving))
    await pipeline.indexer().index(leaving, item_id, last_try=False)
    other_media = await pipeline.store.put(staying, item_id, "video.mp4", b"other user's video")
    assert (tmp_path / "media" / str(leaving)).exists()

    async with session_factory() as session, session.begin():
        await delete_user_data(session, leaving, pipeline.store)

    assert not (tmp_path / "media" / str(leaving)).exists()
    assert await pipeline.store.get(other_media) == b"other user's video"
    async with session_factory() as session:
        runs = await session.execute(
            select(func.count())
            .select_from(EnrichmentRun)
            .where(EnrichmentRun.saved_item_id == item_id)
        )
        assert runs.scalar_one() == 0
        category = await session.execute(
            select(Vocabulary.item_count).where(Vocabulary.value == "car review")
        )
        assert category.scalar_one() == 0
