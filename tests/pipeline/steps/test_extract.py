"""Extract: the text model with the type's schema, and the escalation when no entity was found."""

import uuid
from pathlib import Path

import pytest

from app.models.base import Image, Message, Video
from app.models.fakes import FakeGenerator
from app.pipeline.media.storage import LocalMediaStore
from app.pipeline.steps.extract import Escalation, ExtractionOutputError, Extractor, PostText
from app.pipeline.steps.fetch import MediaKind, StoredMedia, UnavailableReason
from app.types.registry import CONTENT_TYPES
from tests.pipeline.support import (
    ESCALATION_MODEL,
    TEST_PROMPTS,
    movie_extraction,
    open_extraction,
    place_extraction,
    structured_reply,
    study_extraction,
)

NO_MEDIA = StoredMedia.unavailable(UnavailableReason.NO_MEDIA_LINK)
POST = PostText(caption="synthetic caption", collection=None, transcript=None)


def extractor(
    tmp_path: Path, text: FakeGenerator, escalation: FakeGenerator | None = None
) -> Extractor:
    return Extractor(
        text,
        escalation or FakeGenerator([]),
        CONTENT_TYPES,
        TEST_PROMPTS,
        LocalMediaStore(tmp_path),
        temperature=0.0,
        max_output_tokens=800,
    )


async def stored(tmp_path: Path, kind: MediaKind, name: str) -> StoredMedia:
    key = await LocalMediaStore(tmp_path).put(uuid.uuid4(), uuid.uuid4(), name, b"synthetic media")
    return StoredMedia(kind=kind, media_key=key, still_key=key)


async def test_the_text_model_gets_the_post_and_the_types_schema(tmp_path: Path) -> None:
    text = FakeGenerator([structured_reply(place_extraction(("Kronotrop", "coffee")))])
    post = PostText(caption="Bebek'te kahve", collection="Food", transcript="çok iyi")

    result = await extractor(tmp_path, text).extract("place", post, NO_MEDIA)

    request = text.requests[0]
    assert request.output_schema is not None
    assert (
        request.output_schema.schema == CONTENT_TYPES.extraction_schema("place").model_json_schema()
    )
    assert request.messages[0] == Message(role="system", content=TEST_PROMPTS.extraction_system)
    assert request.messages[1].content == [
        "collection=Food\ncaption=Bebek'te kahve\ntranscript=çok iyi"
    ]
    assert result.escalation is Escalation.NOT_NEEDED
    assert result.document["entities"] == [
        {"name": "Kronotrop", "area_hint": None, "cuisine": "coffee"}
    ]


async def test_a_list_post_returns_every_entity(tmp_path: Path) -> None:
    titles = ("Inception", "Dune", "Arrival")
    text = FakeGenerator([structured_reply(movie_extraction(*titles))])
    post = PostText(caption="3 films", collection=None, transcript=None)

    result = await extractor(tmp_path, text).extract("movie", post, NO_MEDIA)

    assert [entity["title"] for entity in result.document["entities"]] == list(titles)  # type: ignore[union-attr]
    assert text.requests[0].messages[1].content == [
        "collection=(none)\ncaption=3 films\ntranscript=(none)"
    ]


async def test_a_post_without_a_built_in_type_uses_the_shared_schema(tmp_path: Path) -> None:
    text = FakeGenerator([structured_reply(open_extraction())])

    result = await extractor(tmp_path, text).extract(None, POST, NO_MEDIA)

    assert result.document == open_extraction()
    assert result.escalation is Escalation.NOT_NEEDED


async def test_a_type_without_a_resolver_is_never_escalated(tmp_path: Path) -> None:
    text = FakeGenerator([structured_reply(study_extraction())])
    media = await stored(tmp_path, MediaKind.VIDEO, "video.mp4")

    result = await extractor(tmp_path, text).extract("study", POST, media)

    assert result.escalation is Escalation.NOT_NEEDED


async def test_no_entity_escalates_to_the_video_model_with_the_video(tmp_path: Path) -> None:
    text = FakeGenerator([structured_reply(place_extraction())])
    escalation = FakeGenerator(
        [structured_reply(place_extraction(("Sign Café", None)), model=ESCALATION_MODEL)]
    )
    media = await stored(tmp_path, MediaKind.VIDEO, "video.mp4")

    result = await extractor(tmp_path, text, escalation).extract("place", POST, media)

    assert result.escalation is Escalation.FOUND
    assert result.escalation_model == ESCALATION_MODEL
    assert result.document["entities"][0]["name"] == "Sign Café"  # type: ignore[index]
    content = escalation.requests[0].messages[1].content
    assert isinstance(content, list) and isinstance(content[1], Video)
    assert escalation.requests[0].messages[0].content == (
        TEST_PROMPTS.extraction_system + "\n\n" + TEST_PROMPTS.escalation_system
    )
    assert result.costs.calls == 2


async def test_an_image_post_escalates_with_the_image(tmp_path: Path) -> None:
    text = FakeGenerator([structured_reply(movie_extraction())])
    escalation = FakeGenerator([structured_reply(movie_extraction("Dune"), model=ESCALATION_MODEL)])
    media = await stored(tmp_path, MediaKind.IMAGE, "image.jpg")

    await extractor(tmp_path, text, escalation).extract("movie", POST, media)

    content = escalation.requests[0].messages[1].content
    assert isinstance(content, list) and isinstance(content[1], Image)


async def test_an_escalation_that_finds_nothing_keeps_the_text_extraction(tmp_path: Path) -> None:
    text_document = place_extraction(category="cafe")
    text = FakeGenerator([structured_reply(text_document)])
    escalation = FakeGenerator([structured_reply(place_extraction(category="bakery"))])
    media = await stored(tmp_path, MediaKind.VIDEO, "video.mp4")

    result = await extractor(tmp_path, text, escalation).extract("place", POST, media)

    assert result.escalation is Escalation.NOT_FOUND
    assert result.document == text_document


async def test_without_stored_media_there_is_nothing_to_escalate_with(tmp_path: Path) -> None:
    text = FakeGenerator([structured_reply(place_extraction())])

    result = await extractor(tmp_path, text).extract("place", POST, NO_MEDIA)

    assert result.escalation is Escalation.NO_MEDIA


async def test_a_reply_that_breaks_the_schema_fails_without_quoting_it(tmp_path: Path) -> None:
    secret = "synthetic private caption text"
    broken = {**open_extraction(), "category": "", "unexpected": secret}
    text = FakeGenerator([structured_reply(broken)])

    with pytest.raises(ExtractionOutputError) as raised:
        await extractor(tmp_path, text).extract(None, POST, NO_MEDIA)

    assert secret not in str(raised.value)
    assert "category" in str(raised.value)
