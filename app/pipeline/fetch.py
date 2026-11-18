"""Step 1, fetch: download the media before its link expires, and keep smaller copies.

Only the link the platform sent is used (`CaptureEvent.media_url`), never a
post's public page: reading a page would be scraping (AGENTS.md §6). A reel
shared by DM comes with its public page only (decision record 0002), so it has
no media here and is indexed from its caption.

A video is kept as a smaller MP4 with its audio track, plus a still picture; an
image as a smaller JPEG. Whether the file has an audio track is read here with
ffprobe, from the downloaded original, because that decides speech-to-text.

A link the platform refuses, a file that is too large and a file that is not
video or image media make the media *unavailable*: the item is still indexed
from its text. A timeout, a rate limit or a server error is raised instead, so
the job is tried again while the link may still work.
"""

import asyncio
import tempfile
import uuid
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Final, Self

import httpx

from app.pipeline.media import MediaToolError, MediaTools, MediaToolStartError
from app.pipeline.storage import MediaStore

_HTTPS: Final = "https"
_VIDEO_CONTENT_TYPE_PREFIX: Final = "video/"
_IMAGE_CONTENT_TYPE_PREFIX: Final = "image/"
_ORIGINAL_FILE_NAME: Final = "original"
_STORED_VIDEO_NAME: Final = "video.mp4"
_STORED_STILL_NAME: Final = "still.jpg"
_STORED_IMAGE_NAME: Final = "image.jpg"
VIDEO_MIME_TYPE: Final = "video/mp4"
IMAGE_MIME_TYPE: Final = "image/jpeg"
# Statuses that say the request may work if it is sent again later.
_RETRYABLE_STATUSES: Final = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})


class MediaKind(StrEnum):
    """What the stored media is."""

    VIDEO = "video"
    IMAGE = "image"


class UnavailableReason(StrEnum):
    """Why an item has no stored media. The item is then indexed from its text."""

    NO_MEDIA_LINK = "no_media_link"
    NOT_HTTPS = "not_https"
    LINK_REFUSED = "link_refused"
    TOO_LARGE = "too_large"
    NOT_MEDIA = "not_media"
    UNREADABLE = "unreadable"


class MediaDownloadError(Exception):
    """The download failed in a way that may pass if it is tried again later.

    The message names the reason and the HTTP status, never the link.
    """


@dataclass(frozen=True)
class StoredMedia:
    """What the fetch step kept of an item's media.

    Attributes:
        kind: Video or image; `None` when no media is available.
        unavailable_reason: Why there is none, when `kind` is `None`.
        media_key: The stored video (MP4) or image (JPEG).
        still_key: A still picture of a video; the image itself for an image.
        has_audio: Whether the downloaded file had an audio track.
        duration_seconds: The video's length, when known.
    """

    kind: MediaKind | None
    unavailable_reason: UnavailableReason | None = None
    media_key: str | None = None
    still_key: str | None = None
    has_audio: bool = False
    duration_seconds: float | None = None

    @classmethod
    def unavailable(cls, reason: UnavailableReason) -> Self:
        """Describe an item whose media could not be kept.

        Args:
            reason: Why.

        Returns:
            Media of no kind, without an audio track.
        """
        return cls(kind=None, unavailable_reason=reason)

    @property
    def mime_type(self) -> str | None:
        """Return the MIME type of `media_key`, or `None` without media."""
        if self.kind is MediaKind.VIDEO:
            return VIDEO_MIME_TYPE
        if self.kind is MediaKind.IMAGE:
            return IMAGE_MIME_TYPE
        return None

    def to_output(self) -> dict[str, object]:
        """Return the step's output for `enrichment_run.output`.

        Returns:
            A JSON object with every attribute.
        """
        return {
            "kind": self.kind,
            "unavailable_reason": self.unavailable_reason,
            "media_key": self.media_key,
            "still_key": self.still_key,
            "has_audio": self.has_audio,
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def from_output(cls, output: dict[str, object]) -> Self:
        """Read the step's output back, when a job continues after a retry.

        Args:
            output: What `to_output` returned.

        Returns:
            The stored media.

        Raises:
            ValueError: If the output is not one `to_output` wrote.
        """
        kind = output.get("kind")
        reason = output.get("unavailable_reason")
        duration = output.get("duration_seconds")
        return cls(
            kind=MediaKind(kind) if isinstance(kind, str) else None,
            unavailable_reason=UnavailableReason(reason) if isinstance(reason, str) else None,
            media_key=_text_or_none(output.get("media_key")),
            still_key=_text_or_none(output.get("still_key")),
            has_audio=output.get("has_audio") is True,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        )


class MediaFetcher:
    """Downloads an item's media and stores the copies the pipeline keeps."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        store: MediaStore,
        tools: MediaTools,
        *,
        timeout_seconds: float,
        max_bytes: int,
        stored_height_pixels: int,
    ) -> None:
        """Create the fetcher.

        Args:
            http_client: The client to download with. It sends no credentials.
            store: Where the copies are kept.
            tools: ffprobe and ffmpeg.
            timeout_seconds: Time limit of one download.
            max_bytes: The largest file that is downloaded.
            stored_height_pixels: The height of the stored copies.
        """
        self._http_client = http_client
        self._store = store
        self._tools = tools
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._stored_height = stored_height_pixels

    async def fetch(
        self, user_id: uuid.UUID, saved_item_id: uuid.UUID, media_url: str | None
    ) -> StoredMedia:
        """Download an item's media and keep its copies.

        Args:
            user_id: The item's owner.
            saved_item_id: The item.
            media_url: The link the platform sent; `None` if it sent none.

        Returns:
            What was kept, or why nothing was.

        Raises:
            MediaDownloadError: If the download may work when tried again.
            MediaToolError: If ffmpeg could not make a copy of a readable file.
            OSError: If a copy could not be stored.
        """
        if media_url is None:
            return StoredMedia.unavailable(UnavailableReason.NO_MEDIA_LINK)
        if httpx.URL(media_url).scheme != _HTTPS:
            return StoredMedia.unavailable(UnavailableReason.NOT_HTTPS)
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / _ORIGINAL_FILE_NAME
            download = await self._download(media_url, original)
            if isinstance(download, UnavailableReason):
                return StoredMedia.unavailable(download)
            return await self._keep_copies(user_id, saved_item_id, original, download)

    async def _download(self, media_url: str, target: Path) -> MediaKind | UnavailableReason:
        try:
            async with self._http_client.stream(
                "GET", media_url, timeout=self._timeout_seconds, follow_redirects=True
            ) as response:
                refusal = _refusal(response)
                if refusal is not None:
                    return refusal
                kind = _media_kind(response.headers.get("content-type"))
                if kind is None:
                    return UnavailableReason.NOT_MEDIA
                data = await self._read_limited(response)
        except httpx.TransportError as error:
            raise MediaDownloadError(f"media download failed ({type(error).__name__})") from error
        if data is None:
            return UnavailableReason.TOO_LARGE
        await asyncio.to_thread(target.write_bytes, data)
        return kind

    async def _read_limited(self, response: httpx.Response) -> bytes | None:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > self._max_bytes:
                return None
        return bytes(data)

    async def _keep_copies(
        self, user_id: uuid.UUID, saved_item_id: uuid.UUID, original: Path, kind: MediaKind
    ) -> StoredMedia:
        try:
            probe = await self._tools.probe(original)
        except MediaToolStartError:
            raise
        except MediaToolError:
            return StoredMedia.unavailable(UnavailableReason.UNREADABLE)
        if not probe.has_video:
            return StoredMedia.unavailable(UnavailableReason.NOT_MEDIA)
        folder = original.parent
        if kind is MediaKind.IMAGE:
            image = folder / _STORED_IMAGE_NAME
            await self._tools.make_still(original, image, self._stored_height)
            image_key = await self._store_file(user_id, saved_item_id, image)
            return StoredMedia(kind=kind, media_key=image_key, still_key=image_key)
        video = folder / _STORED_VIDEO_NAME
        still = folder / _STORED_STILL_NAME
        await self._tools.make_video_copy(original, video, self._stored_height)
        await self._tools.make_still(original, still, self._stored_height)
        return StoredMedia(
            kind=kind,
            media_key=await self._store_file(user_id, saved_item_id, video),
            still_key=await self._store_file(user_id, saved_item_id, still),
            has_audio=probe.has_audio,
            duration_seconds=probe.duration_seconds,
        )

    async def _store_file(self, user_id: uuid.UUID, saved_item_id: uuid.UUID, path: Path) -> str:
        data = await asyncio.to_thread(path.read_bytes)
        return await self._store.put(user_id, saved_item_id, path.name, data)


def _refusal(response: httpx.Response) -> UnavailableReason | None:
    status = response.status_code
    if status in _RETRYABLE_STATUSES or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        raise MediaDownloadError(f"media download failed with HTTP {status}")
    if status >= HTTPStatus.BAD_REQUEST:
        return UnavailableReason.LINK_REFUSED
    return None


def _media_kind(content_type: str | None) -> MediaKind | None:
    if content_type is None:
        return None
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type.startswith(_VIDEO_CONTENT_TYPE_PREFIX):
        return MediaKind.VIDEO
    if media_type.startswith(_IMAGE_CONTENT_TYPE_PREFIX):
        return MediaKind.IMAGE
    return None


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
