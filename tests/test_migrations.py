"""Checks that the Alembic migrations run both ways on a clean database.

These tests need Postgres with PostGIS and pgvector. See `tests/database.py`.
"""

from pathlib import Path

from sqlalchemy import URL

from tests.database import fetch_all, run_alembic

SCHEMA_TABLES = {
    "user",
    "platform_account",
    "saved_item",
    "entity",
    "item_entity",
    "user_entity",
    "enrichment_run",
    "conversation_msg",
}
# Alembic's own bookkeeping table and the table PostGIS creates for itself.
TABLES_OUTSIDE_THE_SCHEMA = {"alembic_version", "spatial_ref_sys"}
REQUIRED_EXTENSIONS = {"postgis", "vector"}
OTHER_EMBEDDING_DIMENSION = 12

LIST_TABLES = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
LIST_EXTENSIONS = "SELECT extname FROM pg_extension"
EMBEDDING_COLUMN_TYPE = (
    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
    "WHERE attrelid = 'saved_item'::regclass AND attname = 'embedding'"
)


def schema_tables(database_url: URL) -> set[object]:
    tables = {row[0] for row in fetch_all(database_url, LIST_TABLES)}
    return tables - TABLES_OUTSIDE_THE_SCHEMA


def test_upgrade_head_creates_every_table_and_extension_on_a_clean_database(
    empty_database_url: URL, tmp_path: Path
) -> None:
    run_alembic(empty_database_url, tmp_path, "upgrade", "head")

    assert schema_tables(empty_database_url) == SCHEMA_TABLES
    extensions = {row[0] for row in fetch_all(empty_database_url, LIST_EXTENSIONS)}
    assert extensions >= REQUIRED_EXTENSIONS


def test_downgrade_base_removes_every_table_and_upgrade_works_again(
    empty_database_url: URL, tmp_path: Path
) -> None:
    run_alembic(empty_database_url, tmp_path, "upgrade", "head")

    run_alembic(empty_database_url, tmp_path, "downgrade", "base")

    assert schema_tables(empty_database_url) == set()
    run_alembic(empty_database_url, tmp_path, "upgrade", "head")
    assert schema_tables(empty_database_url) == SCHEMA_TABLES


def test_embedding_column_gets_the_configured_dimension(
    empty_database_url: URL, tmp_path: Path
) -> None:
    run_alembic(
        empty_database_url,
        tmp_path,
        "upgrade",
        "head",
        embedding_dimension=OTHER_EMBEDDING_DIMENSION,
    )

    rows = fetch_all(empty_database_url, EMBEDDING_COLUMN_TYPE)
    assert rows[0][0] == f"vector({OTHER_EMBEDDING_DIMENSION})"


def test_models_describe_the_same_schema_as_the_migrations(
    migrated_database_url: URL, tmp_path: Path
) -> None:
    # `alembic check` exits with an error when the models and the database differ.
    run_alembic(migrated_database_url, tmp_path, "check")
