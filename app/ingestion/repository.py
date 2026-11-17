"""Database work for ingestion: users on first contact, saved items, collection hints.

Every function runs inside the caller's transaction and never commits.

A saved item is stored once per post id and once per message id (decision
record 0007). Both rules are unique keys in the database, so a second share of
a post, or a webhook that Meta delivers twice, finds the item that is already
there instead of creating another.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import CaptureEvent
from app.db.models import PlatformAccount, SavedItem, User

logger = logging.getLogger(__name__)

# Seed of the 64-bit hash that turns an account into an advisory-lock key.
_LOCK_HASH_SEED = 0


@dataclass(frozen=True)
class StoredCapture:
    """The saved item a capture event belongs to.

    Attributes:
        saved_item_id: The item's id.
        status: The item's place in the indexing pipeline.
        created: Whether this capture created the item. `False` means the
            post, or the message, was already stored.
    """

    saved_item_id: uuid.UUID
    status: str
    created: bool


async def find_or_create_user(
    session: AsyncSession, platform: str, platform_user_id: str
) -> uuid.UUID:
    """Return our user for a platform account, creating both on first contact.

    Two first messages from one account can arrive at the same moment. The
    second one waits for the first and then finds its user, so an account never
    gets two users.

    Args:
        session: The session to work in.
        platform: The platform the account is on.
        platform_user_id: The platform's id of the account.

    Returns:
        The user's id.
    """
    user_id = await _find_user_id(session, platform, platform_user_id)
    if user_id is not None:
        return user_id
    await _lock_first_contact(session, platform, platform_user_id)
    user_id = await _find_user_id(session, platform, platform_user_id)
    if user_id is not None:
        return user_id
    user = User()
    session.add(user)
    await session.flush()
    session.add(
        PlatformAccount(user_id=user.id, platform=platform, platform_user_id=platform_user_id)
    )
    await session.flush()
    logger.info("user_created", extra={"user_id": str(user.id), "platform": platform})
    return user.id


async def store_capture(session: AsyncSession, event: CaptureEvent) -> StoredCapture:
    """Store a capture event as a saved item, once per post and once per message.

    The media link is not stored. It is passed on to the indexing job, which
    downloads the file before the link expires.

    Args:
        session: The session to work in.
        event: The capture event.

    Returns:
        The new item, or the item that already holds this post or message.

    Raises:
        LookupError: If the database refuses the item as a duplicate but no
            item with its post id or message id exists.
    """
    insert_item = (
        insert(SavedItem)
        .values(
            user_id=event.user_id,
            platform=event.platform,
            platform_post_id=event.platform_post_id,
            post_url=event.post_url,
            media_type=event.media_type,
            caption=event.caption,
            collection=event.collection_hint,
            saved_at=event.received_at,
            source_message_id=event.source_message_id,
        )
        .on_conflict_do_nothing()
        .returning(SavedItem.id, SavedItem.status)
    )
    created = (await session.execute(insert_item)).one_or_none()
    if created is not None:
        return StoredCapture(saved_item_id=created.id, status=created.status, created=True)
    existing = await _find_stored_capture(session, event)
    if existing is None:
        raise LookupError("a saved item was refused as a duplicate, but no matching item exists")
    return StoredCapture(saved_item_id=existing.id, status=existing.status, created=False)


async def attach_collection_hint(
    session: AsyncSession,
    user_id: uuid.UUID,
    platform: str,
    hint: str,
    sent_at: datetime,
    window: timedelta,
) -> bool:
    """Attach a text to the item its sender captured just before, as the collection hint.

    The text is a hint when the user's latest capture is at most `window` older
    than the text and has no collection yet. A text that equals the collection
    the item already has is the same hint delivered again.

    Args:
        session: The session to work in.
        user_id: The user who wrote the text.
        platform: The platform the text was sent on.
        hint: The text, stored as the user wrote it.
        sent_at: When the user sent the text.
        window: How long after a capture a text still counts as its hint.

    Returns:
        Whether the text is the collection hint of a just-captured item. `False`
        means it is an ordinary chat message.
    """
    latest_capture = (
        select(SavedItem)
        .where(
            SavedItem.user_id == user_id,
            SavedItem.platform == platform,
            SavedItem.source_message_id.is_not(None),
            SavedItem.saved_at.between(sent_at - window, sent_at),
        )
        .order_by(SavedItem.saved_at.desc())
        .limit(1)
        .with_for_update()
    )
    item = (await session.execute(latest_capture)).scalar_one_or_none()
    if item is None:
        return False
    if item.collection is None:
        item.collection = hint
        await session.flush()
        return True
    return item.collection == hint


async def _find_user_id(
    session: AsyncSession, platform: str, platform_user_id: str
) -> uuid.UUID | None:
    statement = select(PlatformAccount.user_id).where(
        PlatformAccount.platform == platform,
        PlatformAccount.platform_user_id == platform_user_id,
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def _lock_first_contact(session: AsyncSession, platform: str, platform_user_id: str) -> None:
    # Held until the transaction ends. Two accounts whose keys hash to the same
    # number only wait for each other; nothing else depends on the number.
    lock_key = func.hashtextextended(
        f"platform_account:{platform}:{platform_user_id}", _LOCK_HASH_SEED
    )
    await session.execute(select(func.pg_advisory_xact_lock(lock_key)))


async def _find_stored_capture(session: AsyncSession, event: CaptureEvent) -> SavedItem | None:
    # The same two keys as the unique indexes on `saved_item`.
    dedupe_keys: list[ColumnElement[bool]] = []
    if event.platform_post_id is not None:
        dedupe_keys.append(
            and_(
                SavedItem.platform == event.platform,
                SavedItem.platform_post_id == event.platform_post_id,
            )
        )
    if event.source_message_id is not None:
        dedupe_keys.append(SavedItem.source_message_id == event.source_message_id)
    if not dedupe_keys:
        return None
    statement = select(SavedItem).where(SavedItem.user_id == event.user_id, or_(*dedupe_keys))
    return (await session.execute(statement.limit(1))).scalar_one_or_none()
