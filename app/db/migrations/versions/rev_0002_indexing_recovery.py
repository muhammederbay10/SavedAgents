"""Track each saved item's indexing tries and last indexing activity (SAV-14).

Revision: 0002
Revises: 0001

The stuck-item sweeper (record 0012) needs two facts that no existing column
holds: how many indexing runs an item has started, across requeues, and when
an indexing job or the sweeper last worked on it. `saved_at` is the share time
and `enrichment_run` has no timestamps. Both columns belong to the item and are
deleted with it. Existing rows get no tries and the time of this migration.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_UNFINISHED = sa.text("status IN ('pending', 'enriching')")


def upgrade() -> None:
    """Add the two columns and the index the sweeper's query uses."""
    op.add_column(
        "saved_item",
        sa.Column("indexing_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "saved_item",
        sa.Column(
            "indexing_updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_saved_item_unfinished_since"),
        "saved_item",
        ["indexing_updated_at"],
        postgresql_where=_UNFINISHED,
    )


def downgrade() -> None:
    """Drop the index and the two columns."""
    op.drop_index(op.f("ix_saved_item_unfinished_since"), table_name="saved_item")
    op.drop_column("saved_item", "indexing_updated_at")
    op.drop_column("saved_item", "indexing_attempts")
