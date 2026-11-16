"""SQLAlchemy models for the tables in `docs/ARCHITECTURE.md` §4.

The Alembic migrations in `app/db/migrations/` create the schema. These models
describe the same tables for queries, and `tests/test_migrations.py` fails when
the two drift apart.

Deleting a `user` row deletes everything that belongs to that user: platform
accounts, saved items (with their entity links and enrichment runs), entity
statuses and conversation messages. `entity` rows are shared between users and
stay.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import GeographyPoint, GeoPoint, Vector

# JSON columns hold free-form objects whose keys depend on the content type, so
# their values cannot be typed more narrowly than `Any`.
JsonObject = dict[str, Any]

# Postgres text-search configuration for the full-text index. "simple" splits
# and lowercases words without language-specific stemming, because saved posts
# mix Turkish and English.
FULL_TEXT_SEARCH_CONFIG = "simple"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_GENERATED_UUID = text("gen_random_uuid()")
_EMPTY_JSON_OBJECT = text("'{}'::jsonb")
_EMPTY_TEXT_ARRAY = text("'{}'::text[]")
_DELETE_WITH_PARENT = "CASCADE"


class ItemStatus(StrEnum):
    """Where a saved item is in the indexing pipeline."""

    PENDING = "pending"
    ENRICHING = "enriching"
    NEEDS_USER = "needs_user"
    DONE = "done"
    FAILED = "failed"


class EntityStatus(StrEnum):
    """What a user did with an entity: still wants to, or already did."""

    WANT = "want"
    DONE = "done"


def _one_of(column_name: str, values: type[StrEnum]) -> str:
    quoted_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({quoted_values})"


class Base(DeclarativeBase):
    """Base class of every model. Its metadata names constraints predictably."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    """A person who uses SavedAgent."""

    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_GENERATED_UUID
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    home_city: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlatformAccount(Base):
    """A user's account on one platform, e.g. their Instagram account."""

    __tablename__ = "platform_account"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_GENERATED_UUID
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete=_DELETE_WITH_PARENT))
    platform: Mapped[str] = mapped_column(Text)
    platform_user_id: Mapped[str] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)


class SavedItem(Base):
    """One post a user saved, with everything indexing learned about it."""

    __tablename__ = "saved_item"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", "platform_post_id"),
        CheckConstraint(_one_of("status", ItemStatus), name="status_is_known"),
        Index("ix_saved_item_user_id_source_message_id", "user_id", "source_message_id"),
        Index("ix_saved_item_fields", "fields", postgresql_using="gin"),
        Index(
            "ix_saved_item_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_GENERATED_UUID
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete=_DELETE_WITH_PARENT))
    platform: Mapped[str] = mapped_column(Text)
    platform_post_id: Mapped[str | None] = mapped_column(Text)
    post_url: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    subtype: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=_EMPTY_TEXT_ARRAY)
    collection: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[JsonObject] = mapped_column(JSONB, server_default=_EMPTY_JSON_OBJECT)
    attributes: Mapped[JsonObject] = mapped_column(JSONB, server_default=_EMPTY_JSON_OBJECT)
    status: Mapped[str] = mapped_column(Text, server_default=ItemStatus.PENDING)
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source_message_id: Mapped[str | None] = mapped_column(Text)


def saved_item_search_vector() -> ColumnElement[str]:
    """Build the text-search vector over a saved item's caption, summary and transcript.

    Keyword search (fallback level L1) must filter on exactly this expression,
    because Postgres only uses the full-text index for the expression it was
    built on.

    Returns:
        A `tsvector` SQL expression for use in queries on `saved_item`.
    """
    columns = SavedItem.__table__.c
    empty = text("''")
    separator = text("' '")
    caption = func.coalesce(columns.caption, empty)
    summary = func.coalesce(columns.summary, empty)
    transcript = func.coalesce(columns.transcript, empty)
    # `op("||")` groups the chain from the left, the way Postgres stores the
    # expression. `alembic check` compares the two as text, and with the flat
    # form that `concat()` renders it reports the index as changed.
    searchable_text = (
        caption.op("||")(separator).op("||")(summary).op("||")(separator).op("||")(transcript)
    )
    config = text(f"'{FULL_TEXT_SEARCH_CONFIG}'::regconfig")
    return func.to_tsvector(config, searchable_text, type_=TSVECTOR)


Index("ix_saved_item_full_text", saved_item_search_vector(), postgresql_using="gin")


class Entity(Base):
    """A real-world thing a post is about: a restaurant, a movie, a book.

    Shared by all users, one row per `(source, external_id)`.
    """

    __tablename__ = "entity"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_entity_location", "location", postgresql_using="gist"),
        Index("ix_entity_data", "data", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_GENERATED_UUID
    )
    type: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    data: Mapped[JsonObject] = mapped_column(JSONB, server_default=_EMPTY_JSON_OBJECT)
    location: Mapped[GeoPoint | None] = mapped_column(GeographyPoint())
    district: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ItemEntity(Base):
    """Links a saved item to an entity it mentions. A list post has several."""

    __tablename__ = "item_entity"

    saved_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("saved_item.id", ondelete=_DELETE_WITH_PARENT), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity.id", ondelete=_DELETE_WITH_PARENT), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    confidence: Mapped[float | None] = mapped_column(Float)


class UserEntity(Base):
    """One user's status, note and rating for an entity."""

    __tablename__ = "user_entity"
    __table_args__ = (CheckConstraint(_one_of("status", EntityStatus), name="status_is_known"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete=_DELETE_WITH_PARENT), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entity.id", ondelete=_DELETE_WITH_PARENT), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)


class EnrichmentRun(Base):
    """The result of one indexing step for one saved item."""

    __tablename__ = "enrichment_run"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_GENERATED_UUID
    )
    saved_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("saved_item.id", ondelete=_DELETE_WITH_PARENT)
    )
    step: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    output: Mapped[JsonObject | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class ConversationMessage(Base):
    """One chat message between a user and the bot."""

    __tablename__ = "conversation_msg"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_GENERATED_UUID
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete=_DELETE_WITH_PARENT))
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
