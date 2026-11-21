"""Instagram adapter: checks a `messages` webhook and maps it to our own objects.

Meta sends one webhook per direct message to the bot account. What the payloads
really contain is written up in decision records 0002 (shares by DM) and 0003
(saves into a collaborative collection):

| Attachment type | What it is | Post id | `url` |
|---|---|---|---|
| `ig_reel` | A reel shared by DM | `reel_video_id` | The reel's public page |
| `ig_post` | A post shared by DM or saved into a collection | `ig_post_media_id` | A signed image |
| `share` | The older form of `ig_post` | none | A signed media file |
| `unsupported_type` | A reel saved into a collection | none | A signed video file |

A public page link becomes the `post_url`. A signed link becomes the
`media_url` and never the `post_url`. A message with text and no share is a
chat message. A tap on a quick reply arrives as a text (the button's label)
with `message.quick_reply.payload`, which the chat message keeps. Everything
else Meta sends (read receipts, reactions, echoes of the bot's own messages,
other attachment types) is ignored and logged without its content.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ValidationError

from app.adapters.base import CaptureEvent, CaptureSource, ChatMessage, MediaType, Platform

logger = logging.getLogger(__name__)

PLATFORM: Platform = "instagram"
# Shares and saves into a collaborative collection both arrive as direct messages.
_CAPTURE_SOURCE: CaptureSource = "dm"

_WEBHOOK_OBJECT = "instagram"
_SIGNATURE_PREFIX = "sha256="
_MILLISECONDS_PER_SECOND = 1000

# Meta is replacing `share` with `ig_post` during 2026, so both are accepted.
# `unsupported_type` is undocumented: it is what a reel saved into a
# collaborative collection arrives as (decision record 0003).
_MEDIA_TYPE_BY_ATTACHMENT_TYPE: dict[str, MediaType] = {
    "ig_reel": "reel",
    "ig_post": "post",
    "share": "post",
    "unsupported_type": "reel",
}

# Hosts of a post's public page. A link to any other host is a media file.
_PUBLIC_POST_HOSTS = frozenset({"instagram.com", "www.instagram.com"})


class InvalidWebhookPayloadError(ValueError):
    """The webhook body is not the JSON object Meta documents for webhooks."""


@dataclass(frozen=True)
class InstagramShare:
    """A post or reel a user shared with the bot, or saved into a shared collection.

    Attributes:
        sender_id: The sender's app-scoped Instagram id.
        message_id: The message's id (`mid`).
        sent_at: When the user sent the message.
        media_type: What kind of post it is.
        platform_post_id: Instagram's id of the post, when the payload has one.
        post_url: The post's public page, when the payload has one.
        media_url: A signed link to the image or video file, when the payload has one.
        caption: The post's caption, when the payload has one.
    """

    sender_id: str
    message_id: str
    sent_at: datetime
    media_type: MediaType
    platform_post_id: str | None
    post_url: str | None
    media_url: str | None
    caption: str | None


@dataclass(frozen=True)
class InstagramText:
    """A plain text a user wrote to the bot.

    Attributes:
        sender_id: The sender's app-scoped Instagram id.
        message_id: The message's id (`mid`).
        sent_at: When the user sent the message.
        text: What the user wrote, or the label of the button they tapped.
        quick_reply_payload: The tapped button's payload; `None` for typed text.
    """

    sender_id: str
    message_id: str
    sent_at: datetime
    text: str
    quick_reply_payload: str | None = None


InstagramMessage = InstagramShare | InstagramText


def _from_milliseconds(value: object) -> datetime:
    # `bool` is a subclass of `int`, and `true` is not a timestamp.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("a timestamp must be a whole number of milliseconds")
    try:
        return datetime.fromtimestamp(value / _MILLISECONDS_PER_SECOND, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("the timestamp is outside the range of dates") from error


class _Participant(BaseModel):
    id: str


class _AttachmentPayload(BaseModel):
    url: str | None = None
    title: str | None = None
    reel_video_id: str | None = None
    ig_post_media_id: str | None = None


class _Attachment(BaseModel):
    type: str
    payload: _AttachmentPayload | None = None


class _QuickReply(BaseModel):
    payload: str | None = None


class _Message(BaseModel):
    mid: str
    text: str | None = None
    quick_reply: _QuickReply | None = None
    attachments: list[_Attachment] = []
    is_echo: bool = False
    is_deleted: bool = False


class _MessagingEvent(BaseModel):
    sender: _Participant
    timestamp: Annotated[datetime, BeforeValidator(_from_milliseconds)]
    message: _Message | None = None


class _Entry(BaseModel):
    # Each event is validated on its own, so one event in a shape we don't know
    # can't make us drop the other events of the same webhook.
    messaging: list[dict[str, object]] = []


class _Webhook(BaseModel):
    object: str
    entry: list[_Entry]


def is_valid_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Check Meta's `X-Hub-Signature-256` header against the body as it was received.

    Args:
        raw_body: The request body, byte for byte. A parsed and re-serialized
            body would not match the signature.
        signature_header: The header's value, or `None` when it was not sent.
        app_secret: The Meta app's secret.

    Returns:
        Whether the header is `sha256=` followed by the HMAC-SHA256 of the body.
    """
    if signature_header is None or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix(_SIGNATURE_PREFIX)
    return hmac.compare_digest(expected.encode(), received.encode())


def parse_webhook(raw_body: bytes) -> list[InstagramMessage]:
    """Read the shares and texts out of one webhook body.

    Call it only after `is_valid_signature` accepted the body.

    Args:
        raw_body: The request body.

    Returns:
        The messages to ingest, in the order Meta sent them. Events that are
        not a share or a text are left out.

    Raises:
        InvalidWebhookPayloadError: If the body is not a webhook object.
    """
    try:
        webhook = _Webhook.model_validate_json(raw_body)
    except ValidationError as error:
        # Not chained: Pydantic's message repeats parts of the body, which is personal data.
        raise InvalidWebhookPayloadError(
            f"the body has {error.error_count()} validation error(s)"
        ) from None
    if webhook.object != _WEBHOOK_OBJECT:
        logger.warning("instagram_webhook_ignored", extra={"reason": "other_object"})
        return []
    events = (event for entry in webhook.entry for event in entry.messaging)
    return [message for event in events if (message := _parse_event(event)) is not None]


def to_capture_event(share: InstagramShare, user_id: UUID) -> CaptureEvent:
    """Build the platform-independent capture event for a share.

    Args:
        share: The share read from the webhook.
        user_id: Our user the sender belongs to.

    Returns:
        The capture event. The collection hint is empty: on Instagram it
        arrives later, as a separate text.
    """
    return CaptureEvent(
        user_id=user_id,
        platform=PLATFORM,
        source=_CAPTURE_SOURCE,
        post_url=share.post_url,
        platform_post_id=share.platform_post_id,
        caption=share.caption,
        media_url=share.media_url,
        media_type=share.media_type,
        collection_hint=None,
        source_message_id=share.message_id,
        received_at=share.sent_at,
    )


def to_chat_message(text: InstagramText, user_id: UUID) -> ChatMessage:
    """Build the platform-independent chat message for a text.

    Args:
        text: The text read from the webhook.
        user_id: Our user the sender belongs to.

    Returns:
        The chat message.
    """
    return ChatMessage(
        user_id=user_id,
        platform=PLATFORM,
        text=text.text,
        message_id=text.message_id,
        received_at=text.sent_at,
        quick_reply_payload=text.quick_reply_payload,
    )


def _parse_event(raw_event: dict[str, object]) -> InstagramMessage | None:
    try:
        event = _MessagingEvent.model_validate(raw_event)
    except ValidationError as error:
        invalid_fields = sorted({".".join(str(part) for part in e["loc"]) for e in error.errors()})
        logger.warning("instagram_event_invalid", extra={"invalid_fields": invalid_fields})
        return None
    message = event.message
    skip_reason = _reason_to_skip(message)
    if message is None or skip_reason is not None:
        logger.info("instagram_event_ignored", extra={"reason": skip_reason})
        return None
    content = _share_in(event, message) or _text_in(event, message)
    if content is None:
        attachment_types = sorted({attachment.type for attachment in message.attachments})
        logger.info(
            "instagram_event_ignored",
            extra={"reason": "no_share_or_text", "attachment_types": attachment_types},
        )
    return content


def _reason_to_skip(message: _Message | None) -> str | None:
    if message is None:
        return "not_a_message"
    if message.is_echo or message.is_deleted:
        return "echo_or_deleted"
    if _text_or_none(message.mid) is None:
        return "no_message_id"
    return None


def _share_in(event: _MessagingEvent, message: _Message) -> InstagramShare | None:
    shares = [
        share
        for attachment in message.attachments
        if (share := _share_from(attachment, event, message)) is not None
    ]
    if not shares:
        return None
    if len(shares) > 1:
        # One saved item per message (decision record 0007). No recorded message
        # holds two shares, so this is logged loudly instead of guessed at.
        logger.warning("instagram_extra_shares_ignored", extra={"ignored_count": len(shares) - 1})
    return shares[0]


def _share_from(
    attachment: _Attachment, event: _MessagingEvent, message: _Message
) -> InstagramShare | None:
    media_type = _MEDIA_TYPE_BY_ATTACHMENT_TYPE.get(attachment.type)
    payload = attachment.payload
    if media_type is None or payload is None:
        return None
    url = _text_or_none(payload.url)
    # Meta sends `reel_video_id` only for reels and `ig_post_media_id` only for posts.
    post_id = _text_or_none(payload.reel_video_id) or _text_or_none(payload.ig_post_media_id)
    if url is None and post_id is None:
        return None
    is_public_page = url is not None and urlsplit(url).hostname in _PUBLIC_POST_HOSTS
    return InstagramShare(
        sender_id=event.sender.id,
        message_id=message.mid,
        sent_at=event.timestamp,
        media_type=media_type,
        platform_post_id=post_id,
        post_url=url if is_public_page else None,
        media_url=None if is_public_page else url,
        caption=_text_or_none(payload.title),
    )


def _text_in(event: _MessagingEvent, message: _Message) -> InstagramText | None:
    text = _text_or_none(message.text)
    if text is None:
        return None
    payload = message.quick_reply.payload if message.quick_reply is not None else None
    return InstagramText(
        sender_id=event.sender.id,
        message_id=message.mid,
        sent_at=event.timestamp,
        text=text,
        quick_reply_payload=_text_or_none(payload),
    )


def _text_or_none(value: str | None) -> str | None:
    return value if value and value.strip() else None
