"""Fetch: download the media from the link the platform sent, and keep smaller copies."""

import uuid
from pathlib import Path

import httpx
import pytest

from app.pipeline.media.storage import LocalMediaStore
from app.pipeline.media.tools import MediaProbe, MediaToolError, MediaToolStartError
from app.pipeline.steps.fetch import (
    MediaDownloadError,
    MediaFetcher,
    MediaKind,
    StoredMedia,
    UnavailableReason,
)
from tests.pipeline.support import IMAGE_BYTES, MEDIA_URL, VIDEO_BYTES, FakeMediaTools, MediaServer

STORED_HEIGHT = 360
MAX_BYTES = 1000
USER_ID = uuid.uuid4()
ITEM_ID = uuid.uuid4()


def fetcher(
    client: httpx.AsyncClient, store: LocalMediaStore, tools: FakeMediaTools
) -> MediaFetcher:
    return MediaFetcher(
        client,
        store,
        tools,
        timeout_seconds=5,
        max_bytes=MAX_BYTES,
        stored_height_pixels=STORED_HEIGHT,
    )


async def test_a_video_is_kept_as_a_smaller_copy_and_a_still(tmp_path: Path) -> None:
    server = MediaServer(VIDEO_BYTES, "video/mp4")
    store, tools = LocalMediaStore(tmp_path), FakeMediaTools()

    media = await fetcher(server.client(), store, tools).fetch(USER_ID, ITEM_ID, MEDIA_URL)

    assert media.kind is MediaKind.VIDEO
    assert media.has_audio
    assert media.duration_seconds == 12.5
    assert media.media_key == f"{USER_ID}/{ITEM_ID}/video.mp4"
    assert await store.get(media.media_key) == b"video copy 360"
    assert media.still_key is not None
    assert await store.get(media.still_key) == b"still 360"
    assert tools.calls == ["probe", "make_video_copy", "make_still"]


async def test_the_audio_track_check_comes_from_ffprobe(tmp_path: Path) -> None:
    server = MediaServer(VIDEO_BYTES, "video/mp4")
    tools = FakeMediaTools(MediaProbe(has_video=True, has_audio=False, duration_seconds=8))

    media = await fetcher(server.client(), LocalMediaStore(tmp_path), tools).fetch(
        USER_ID, ITEM_ID, MEDIA_URL
    )

    assert not media.has_audio


async def test_an_image_is_kept_as_a_smaller_image(tmp_path: Path) -> None:
    server = MediaServer(IMAGE_BYTES, "image/jpeg")
    tools = FakeMediaTools(MediaProbe(has_video=True, has_audio=False, duration_seconds=None))

    media = await fetcher(server.client(), LocalMediaStore(tmp_path), tools).fetch(
        USER_ID, ITEM_ID, MEDIA_URL
    )

    assert media.kind is MediaKind.IMAGE
    assert media.media_key == media.still_key == f"{USER_ID}/{ITEM_ID}/image.jpg"
    assert media.mime_type == "image/jpeg"
    assert not media.has_audio


@pytest.mark.parametrize(
    ("media_url", "reason"),
    [
        (None, UnavailableReason.NO_MEDIA_LINK),
        ("http://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1", UnavailableReason.NOT_HTTPS),
    ],
)
async def test_without_a_usable_link_nothing_is_downloaded(
    tmp_path: Path, media_url: str | None, reason: UnavailableReason
) -> None:
    server = MediaServer(VIDEO_BYTES, "video/mp4")

    media = await fetcher(server.client(), LocalMediaStore(tmp_path), FakeMediaTools()).fetch(
        USER_ID, ITEM_ID, media_url
    )

    assert media == StoredMedia.unavailable(reason)
    assert server.requests == 0


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (MediaServer(b"", "text/html", status=403), UnavailableReason.LINK_REFUSED),
        (MediaServer(b"", "text/html", status=404), UnavailableReason.LINK_REFUSED),
        (MediaServer(b"<html>", "text/html"), UnavailableReason.NOT_MEDIA),
        (MediaServer(b"x" * (MAX_BYTES + 1), "video/mp4"), UnavailableReason.TOO_LARGE),
    ],
    ids=["forbidden", "gone", "html-page", "too-large"],
)
async def test_unusable_media_is_unavailable_and_the_item_goes_on(
    tmp_path: Path, server: MediaServer, reason: UnavailableReason
) -> None:
    media = await fetcher(server.client(), LocalMediaStore(tmp_path), FakeMediaTools()).fetch(
        USER_ID, ITEM_ID, MEDIA_URL
    )

    assert media.kind is None
    assert media.unavailable_reason is reason


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_a_temporary_failure_is_raised_to_try_again(tmp_path: Path, status: int) -> None:
    server = MediaServer(b"", "text/plain", status=status)

    with pytest.raises(MediaDownloadError, match=str(status)) as raised:
        await fetcher(server.client(), LocalMediaStore(tmp_path), FakeMediaTools()).fetch(
            USER_ID, ITEM_ID, MEDIA_URL
        )

    assert MEDIA_URL not in str(raised.value)


async def test_a_network_error_is_raised_to_try_again(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    with pytest.raises(MediaDownloadError):
        await fetcher(client, LocalMediaStore(tmp_path), FakeMediaTools()).fetch(
            USER_ID, ITEM_ID, MEDIA_URL
        )


class UnreadableTools(FakeMediaTools):
    async def probe(self, source: Path) -> MediaProbe:
        raise MediaToolError("ffprobe failed with exit code 1")


class MissingTools(FakeMediaTools):
    async def probe(self, source: Path) -> MediaProbe:
        raise MediaToolStartError("ffprobe could not be started (FileNotFoundError)")


async def test_a_file_ffprobe_cannot_read_is_unavailable(tmp_path: Path) -> None:
    server = MediaServer(VIDEO_BYTES, "video/mp4")

    media = await fetcher(server.client(), LocalMediaStore(tmp_path), UnreadableTools()).fetch(
        USER_ID, ITEM_ID, MEDIA_URL
    )

    assert media.unavailable_reason is UnavailableReason.UNREADABLE


async def test_a_missing_ffprobe_is_an_error_not_an_unreadable_file(tmp_path: Path) -> None:
    server = MediaServer(VIDEO_BYTES, "video/mp4")

    with pytest.raises(MediaToolStartError):
        await fetcher(server.client(), LocalMediaStore(tmp_path), MissingTools()).fetch(
            USER_ID, ITEM_ID, MEDIA_URL
        )


def test_stored_media_survives_its_step_output() -> None:
    media = StoredMedia(
        kind=MediaKind.VIDEO, media_key="a/b/video.mp4", still_key="a/b/still.jpg", has_audio=True
    )

    assert StoredMedia.from_output(media.to_output()) == media
    unavailable = StoredMedia.unavailable(UnavailableReason.TOO_LARGE)
    assert StoredMedia.from_output(unavailable.to_output()) == unavailable
