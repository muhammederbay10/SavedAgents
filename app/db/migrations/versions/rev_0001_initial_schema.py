"""Create the Phase 1 tables from docs/ARCHITECTURE.md §4.

Revision: 0001
Revises: -

The `memory` table comes in Phase 2. The size of `saved_item.embedding` is read
from `EMBEDDING_DIMENSION` when this revision runs. Changing the dimension
later needs a new revision that resizes the column and re-embeds every item.
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import get_settings
from app.db.types import GeographyPoint, Vector

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_GENERATED_UUID = sa.text("gen_random_uuid()")
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")
_NOW = sa.text("now()")
_DELETE_WITH_PARENT = "CASCADE"

_SAVED_ITEM_SEARCH_VECTOR = sa.text(
    "to_tsvector('simple'::regconfig, "
    "coalesce(caption, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(transcript, ''))"
)

# Dropped in this order, so that tables go before the tables they reference.
_TABLES_IN_DROP_ORDER = (
    "conversation_msg",
    "enrichment_run",
    "user_entity",
    "item_entity",
    "entity",
    "saved_item",
    "platform_account",
    "user",
)


def _uuid_primary_key() -> sa.Column[uuid.UUID]:
    return sa.Column("id", sa.Uuid(), server_default=_GENERATED_UUID, nullable=False)


def _create_user_tables() -> None:
    op.create_table(
        "user",
        _uuid_primary_key(),
        sa.Column("display_name", sa.Text()),
        sa.Column("home_city", sa.Text()),
        sa.Column("language", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user"),
    )
    op.create_table(
        "platform_account",
        _uuid_primary_key(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_platform_account"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_platform_account_user_id_user",
            ondelete=_DELETE_WITH_PARENT,
        ),
        sa.UniqueConstraint(
            "platform", "platform_user_id", name="uq_platform_account_platform_platform_user_id"
        ),
    )
    op.create_table(
        "conversation_msg",
        _uuid_primary_key(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_msg"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_conversation_msg_user_id_user",
            ondelete=_DELETE_WITH_PARENT,
        ),
    )


def _create_saved_item_tables(embedding_dimension: int) -> None:
    op.create_table(
        "saved_item",
        _uuid_primary_key(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_post_id", sa.Text()),
        sa.Column("post_url", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text()),
        sa.Column("caption", sa.Text()),
        sa.Column("transcript", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("type", sa.Text()),
        sa.Column("subtype", sa.Text()),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("collection", sa.Text()),
        sa.Column("fields", postgresql.JSONB(), server_default=_EMPTY_JSON_OBJECT, nullable=False),
        sa.Column(
            "attributes", postgresql.JSONB(), server_default=_EMPTY_JSON_OBJECT, nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("embedding", Vector(embedding_dimension)),
        sa.Column("saved_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("source_message_id", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_saved_item"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_saved_item_user_id_user",
            ondelete=_DELETE_WITH_PARENT,
        ),
        sa.UniqueConstraint(
            "user_id",
            "platform",
            "platform_post_id",
            name="uq_saved_item_user_id_platform_platform_post_id",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'enriching', 'needs_user', 'done', 'failed')",
            name=op.f("ck_saved_item_status_is_known"),
        ),
    )
    op.create_index(
        "ix_saved_item_user_id_source_message_id", "saved_item", ["user_id", "source_message_id"]
    )
    op.create_index("ix_saved_item_fields", "saved_item", ["fields"], postgresql_using="gin")
    op.create_index(
        "ix_saved_item_embedding",
        "saved_item",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_saved_item_full_text", "saved_item", [_SAVED_ITEM_SEARCH_VECTOR], postgresql_using="gin"
    )
    op.create_table(
        "enrichment_run",
        _uuid_primary_key(),
        sa.Column("saved_item_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("output", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_enrichment_run"),
        sa.ForeignKeyConstraint(
            ["saved_item_id"],
            ["saved_item.id"],
            name="fk_enrichment_run_saved_item_id_saved_item",
            ondelete=_DELETE_WITH_PARENT,
        ),
    )


def _create_entity_tables() -> None:
    op.create_table(
        "entity",
        _uuid_primary_key(),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), server_default=_EMPTY_JSON_OBJECT, nullable=False),
        sa.Column("location", GeographyPoint()),
        sa.Column("district", sa.Text()),
        sa.Column("city", sa.Text()),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_entity"),
        sa.UniqueConstraint("source", "external_id", name="uq_entity_source_external_id"),
    )
    op.create_index("ix_entity_location", "entity", ["location"], postgresql_using="gist")
    op.create_index("ix_entity_data", "entity", ["data"], postgresql_using="gin")
    op.create_table(
        "item_entity",
        sa.Column("saved_item_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.PrimaryKeyConstraint("saved_item_id", "entity_id", name="pk_item_entity"),
        sa.ForeignKeyConstraint(
            ["saved_item_id"],
            ["saved_item.id"],
            name="fk_item_entity_saved_item_id_saved_item",
            ondelete=_DELETE_WITH_PARENT,
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entity.id"],
            name="fk_item_entity_entity_id_entity",
            ondelete=_DELETE_WITH_PARENT,
        ),
    )
    op.create_table(
        "user_entity",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("rating", sa.Integer()),
        sa.PrimaryKeyConstraint("user_id", "entity_id", name="pk_user_entity"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_entity_user_id_user",
            ondelete=_DELETE_WITH_PARENT,
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entity.id"],
            name="fk_user_entity_entity_id_entity",
            ondelete=_DELETE_WITH_PARENT,
        ),
        sa.CheckConstraint(
            "status IN ('want', 'done')", name=op.f("ck_user_entity_status_is_known")
        ),
    )


def upgrade() -> None:
    """Enable PostGIS and pgvector, then create the tables and their indexes.

    Raises:
        pydantic.ValidationError: If `EMBEDDING_DIMENSION` is missing or invalid.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    _create_user_tables()
    _create_saved_item_tables(get_settings().embedding_dimension)
    _create_entity_tables()


def downgrade() -> None:
    """Drop every table this revision created.

    The PostGIS and pgvector extensions stay. The database image also enables
    them, so they may have been there before this revision ran, and other
    objects in the database may depend on them.
    """
    for table_name in _TABLES_IN_DROP_ORDER:
        op.drop_table(table_name)
