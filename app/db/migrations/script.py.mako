"""${message}.

Revision: ${up_revision}
Revises: ${down_revision | comma,n}
"""

import sqlalchemy as sa
from alembic import op

revision: str = "${up_revision}"
down_revision: str | None = ${f'"{down_revision}"' if down_revision else "None"}
branch_labels: str | tuple[str, ...] | None = ${repr(branch_labels)}
depends_on: str | tuple[str, ...] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Undo this revision."""
    ${downgrades if downgrades else "pass"}
