"""Synthetic rows and small helpers shared by the database tests."""

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Base, Entity, SavedItem, User
from app.db.types import GeoPoint
from tests.database import TEST_EMBEDDING_DIMENSION

INSTAGRAM = "instagram"
REEL_ID = "18000000000000201"
OTHER_REEL_ID = "18000000000000202"
REEL_URL = "https://www.instagram.com/reel/FAKEREEL002/"
OTHER_REEL_URL = "https://www.instagram.com/reel/FAKEREEL006/"
MESSAGE_ID = "aWdfZAGFAKEMID0002"
OTHER_MESSAGE_ID = "aWdfZAGFAKEMID0008"
SENDER_ID = "1000000000000001"


async def add_user(session: AsyncSession) -> User:
    user = User(display_name="Test User")
    session.add(user)
    await session.flush()
    return user


def reel_saved_by(user: User, **overrides: object) -> SavedItem:
    values: dict[str, object] = {
        "user_id": user.id,
        "platform": INSTAGRAM,
        "platform_post_id": REEL_ID,
        "post_url": REEL_URL,
        "media_type": "reel",
    }
    return SavedItem(**{**values, **overrides})


def collection_save_by(user: User, message_id: str) -> SavedItem:
    """A reel saved into a collaborative collection: a message id, no post id and no link."""
    return SavedItem(
        user_id=user.id, platform=INSTAGRAM, media_type="reel", source_message_id=message_id
    )


def place(external_id: str, name: str, location: GeoPoint | None = None) -> Entity:
    return Entity(
        type="place",
        source="google_places",
        external_id=external_id,
        name=name,
        location=location,
    )


def unit_vector(axis: int) -> list[float]:
    return [1.0 if index == axis else 0.0 for index in range(TEST_EMBEDDING_DIMENSION)]


async def count_rows(
    session: AsyncSession, model: type[Base], *conditions: ColumnElement[bool]
) -> int:
    statement = select(func.count()).select_from(model).where(*conditions)
    return (await session.execute(statement)).scalar_one()
