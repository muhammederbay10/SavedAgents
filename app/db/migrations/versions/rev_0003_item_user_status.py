"""Keep the user's status on a saved item that has no entity (SAV-17).

Revision: 0003
Revises: 0002

`set_status` marks the real thing behind an item as done or wanted in
`user_entity`. A study post and other items without a resolver have no entity,
so their status is kept on the item itself (record 0015, owner decision Q6). It
is separate from `status`, the item's indexing lifecycle, and is deleted with
the item. Existing rows have none.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the column and the check on its values."""
    op.add_column("saved_item", sa.Column("user_status", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_saved_item_user_status_is_known"),
        "saved_item",
        "user_status IN ('want', 'done')",
    )


def downgrade() -> None:
    """Drop the check and the column."""
    op.drop_constraint(op.f("ck_saved_item_user_status_is_known"), "saved_item", type_="check")
    op.drop_column("saved_item", "user_status")
