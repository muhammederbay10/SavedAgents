"""Alembic environment: runs the migrations on the database from `DATABASE_URL`.

Alembic imports this module for every command. Settings come from
`app/config.py`, so migrations and the API always use the same database URL
and embedding dimension.
"""

import asyncio

from alembic import context
from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db.models import Base
from app.logging_config import configure_logging

# PostGIS creates this table for itself. It is not part of our schema, so
# `alembic check` and autogenerate must not report it as a table without a model.
_POSTGIS_TABLES = frozenset({"spatial_ref_sys"})


def _belongs_to_our_schema(
    name: str | None, object_type: NameFilterType, parent_names: NameFilterParentNames
) -> bool:
    return not (object_type == "table" and name in _POSTGIS_TABLES)


def _run_migrations_on(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_name=_belongs_to_our_schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations() -> None:
    """Connect to the configured database and run the requested migrations.

    Raises:
        pydantic.ValidationError: If a required setting is missing or invalid.
        sqlalchemy.exc.SQLAlchemyError: If the database rejects a migration.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_async_engine(str(settings.database_url), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations_on)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise NotImplementedError(
        "Offline mode (--sql) is not supported: the migrations need a database connection."
    )

asyncio.run(run_migrations())
