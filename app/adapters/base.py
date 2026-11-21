"""What every capture adapter outputs (`docs/ARCHITECTURE.md` §3.1).

An adapter turns one platform's input into a `CaptureEvent` (something the user
saved) or a `ChatMessage` (something the user wrote to the bot). Everything
after the adapter works on these two objects and knows nothing about the
platform's own payloads.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

Platform = Literal["instagram", "tiktok", "x", "facebook", "youtube"]
CaptureSource = Literal["dm", "share_ext", "browser_import", "api"]
MediaType = Literal["reel", "post", "carousel", "video", "tweet"]

# A missing id or link is `None`, never empty text: an empty text would count as
# a real value in the dedupe keys of `saved_item` (decision record 0007).
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class CaptureEvent(BaseModel):
    """One post a user saved, as it arrived from a platform.

    Attributes:
        user_id: Our user who saved the post.
        platform: The platform the post is on.
        source: How the save reached us.
        post_url: The post's public link. `None` when the platform sends none,
            as for a reel saved into a collaborative collection or a photo post
            shared by DM. A signed media link never goes here.
        platform_post_id: The platform's id of the post, when it sends one.
        caption: The post's caption, when the platform sends it.
        media_url: A link to the video or image file, when the platform sends
            one. It can expire, so it is fetched right away and never stored.
        media_type: What kind of post it is, when that is known.
        collection_hint: The user's collection for the post, in their own
            words, when it is known at capture time.
        source_message_id: The id of the message that carried the save, so
            answers can reply to it. `None` for saves that were not a message.
        received_at: When the user saved or sent the post.
    """

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    platform: Platform
    source: CaptureSource
    post_url: NonEmptyText | None
    platform_post_id: NonEmptyText | None
    caption: str | None
    media_url: NonEmptyText | None
    media_type: MediaType | None
    collection_hint: str | None
    source_message_id: NonEmptyText | None
    received_at: datetime


class ChatMessage(BaseModel):
    """One text a user wrote to the bot.

    Attributes:
        user_id: Our user who wrote the text.
        platform: The platform the text was sent on.
        text: What the user wrote.
        message_id: The platform's id of the message.
        received_at: When the user sent the text.
        quick_reply_payload: The payload of the button the user tapped, when
            the text is a tap on a quick reply; `None` for typed text.
    """

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    platform: Platform
    text: NonEmptyText
    message_id: NonEmptyText
    received_at: datetime
    quick_reply_payload: NonEmptyText | None = None
