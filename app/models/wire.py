"""Small helpers for OpenRouter's JSON: safe reads, media parts and charges.

Responses are untrusted input. Every read goes through a helper that returns
`None` for a missing or mistyped value, so a surprising response becomes a
clear error in the client instead of an `AttributeError` somewhere else.
"""

import base64
import math

from app.models.base import Audio, Image, Media, Video

JsonObject = dict[str, object]


def object_or_none(value: object) -> JsonObject | None:
    """Return the value if it is a JSON object with string keys, else `None`."""
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return None


def list_or_none(value: object) -> list[object] | None:
    """Return the value if it is a JSON array, else `None`."""
    return value if isinstance(value, list) else None


def string_or_none(value: object) -> str | None:
    """Return the value if it is a string, else `None`."""
    return value if isinstance(value, str) else None


def number_or_none(value: object) -> float | None:
    """Return the value as a float if it is a finite JSON number, else `None`.

    JSON parsers accept `NaN` and `Infinity`, and Python counts `True` as an
    integer. None of them is a usable number here.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def cost_usd(body: JsonObject) -> float | None:
    """Return the charge a response reports in `usage.cost`.

    Args:
        body: A response body.

    Returns:
        The charge in US dollars; `None` if it is absent or negative.
    """
    usage = object_or_none(body.get("usage"))
    if usage is None:
        return None
    cost = number_or_none(usage.get("cost"))
    return cost if cost is not None and cost >= 0 else None


def media_part(media: Media) -> JsonObject:
    """Build the content part that carries a media file inline.

    The same shapes are used by chat completions and by the decisions endpoint:
    images and videos travel as base64 data URLs, audio as bare base64 with its
    format.

    Args:
        media: The image, video or audio clip.

    Returns:
        The content part as a JSON object.
    """
    if isinstance(media, Audio):
        return {"type": "input_audio", "input_audio": audio_input(media)}
    if isinstance(media, Image):
        return {"type": "image_url", "image_url": {"url": _data_url(media)}}
    return {"type": "video_url", "video_url": {"url": _data_url(media)}}


def audio_input(audio: Audio) -> JsonObject:
    """Build the `input_audio` object: bare base64 (not a data URL) and the format.

    Args:
        audio: The clip.

    Returns:
        The object used by transcription requests and by audio content parts.
    """
    return {"data": _base64(audio.data), "format": audio.format}


def _data_url(media: Image | Video) -> str:
    return f"data:{media.mime_type};base64,{_base64(media.data)}"


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
