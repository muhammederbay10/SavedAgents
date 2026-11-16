# 0005: Database schema and migrations

- Status: partly accepted. The choices under [Decision](#decision) are made inside SAV-9's scope and take effect when its pull request is merged. The items under [Pending owner decisions](#pending-owner-decisions) need Muhammed's explicit approval and are **not approved yet**.
- Date: 2026-11-17
- Ticket: SAV-9

## Context

`docs/ARCHITECTURE.md` §4 lists the tables and their columns. It doesn't say which columns may be empty, what happens to rows when a user is deleted, or how each index is built. SAV-9 creates the tables, so it has to settle these. Decision record 0004 adds `saved_item.source_message_id`.

## Decision

1. **One migration creates the eight Phase 1 tables:** `user`, `platform_account`, `saved_item`, `entity`, `item_entity`, `user_entity`, `enrichment_run` and `conversation_msg`. `memory` comes in Phase 2. `user` is a reserved word in Postgres, so raw SQL must write it as `"user"`. SQLAlchemy does that by itself.
2. **Migrations are written by hand, and a test compares them with the models.** `app/db/models.py` describes the tables for queries. `tests/test_migrations.py` runs `alembic check`, which fails when the models and a migrated database differ.
3. **The embedding size is read from `EMBEDDING_DIMENSION` when the first migration runs.** The models leave the size out, and the database enforces it. The setting accepts 1 to 2000, because pgvector can't build an HNSW index on a larger `vector` column.
4. **Which columns are required.**
   - Required: every id and owner reference, `platform`, `post_url` (see P1), an entity's `type`, `source`, `external_id` and `name`, both `status` columns, and the role and content of a conversation message.
   - Filled by the database when left out: ids (`gen_random_uuid()`), `created_at`, `saved_at`, `refreshed_at` and `at` (now), `saved_item.status` (`pending`), `tags` (empty list), `fields`, `attributes` and `data` (empty object), `item_entity.position` (0).
   - Everything else may be empty, because indexing fills it in later or not at all: `platform_post_id`, `media_type`, `caption`, `transcript`, `summary`, `type`, `subtype`, `collection`, `embedding`, `source_message_id`, an entity's `location`, `district` and `city`.
5. **The two documented status lists are enforced by the database.** `saved_item.status` must be `pending`, `enriching`, `needs_user`, `done` or `failed`. `user_entity.status` must be `want` or `done`. Other text columns (`platform`, `media_type`, `type`, `enrichment_run.status`, `conversation_msg.role`) are checked in the application, because their values come from registries and adapters that grow without a migration.
6. **Deleting a `user` row deletes all of that user's data:** platform accounts, saved items (with `source_message_id`, entity links and enrichment runs), entity statuses and conversation messages. `entity` rows stay. They describe public things such as a restaurant or a movie, and other users share them.
7. **Indexes.**
   - The ticket's list: unique `(user_id, platform, platform_post_id)` on `saved_item`, unique `(source, external_id)` on `entity`, GIST on `entity.location`, GIN on `entity.data` and `saved_item.fields`, HNSW on `saved_item.embedding`.
   - From decision record 0004: `(user_id, source_message_id)` on `saved_item`.
   - **One addition:** unique `(platform, platform_user_id)` on `platform_account`. Without it, two users could claim the same Instagram account, and the webhook couldn't tell whose message arrived.
   - The HNSW index uses cosine distance (`vector_cosine_ops`, queried with `<=>`).
8. **Full-text search is an index on an expression, not a new column.** The index covers `caption`, `summary` and `transcript` together, with Postgres's `simple` text-search configuration. Queries must use the same expression, so `saved_item_search_vector()` in `app/db/models.py` builds it, and a test checks that Postgres can use the index for it.
9. **The pgvector and PostGIS columns use two small types of our own** (`Vector` and `GeographyPoint` in `app/db/types.py`) instead of the `pgvector` and `GeoAlchemy2` packages. They send and read values as text, which the driver handles without setup. A location is a `GeoPoint(latitude, longitude)` in Python.
10. **`downgrade` leaves the PostGIS and pgvector extensions in place.** The database image enables them too, so the migration can't know that it created them.
11. **Database tests run against a real Postgres and are skipped without one.** They need `TEST_DATABASE_ADMIN_URL`. Each run creates its own databases and drops them. Pytest now lists every skipped test with its reason (`-ra`).

## Pending owner decisions

These are open on SAV-9. The code runs and can be reviewed, but Muhammed has not approved them.

| # | Decision | What the code does today | If it is approved |
|---|---|---|---|
| P1 | How to dedupe collection-saved reels, which have no `platform_post_id` and no post link (records 0003 and 0004). The proposal is in the SAV-9 comments | `post_url` is required. `(user_id, source_message_id)` is a plain index. So a collection-saved reel can't be stored yet | `post_url` may be empty. The index becomes unique for rows that have a message id. A check requires a post id or a message id on every row. This record and `ARCHITECTURE.md` §3.1, §3.2 and §4 are updated |
| P2 | Runtime dependency `asyncpg` | It is the Postgres driver. The ticket and SAV-22 §6 name SQLAlchemy (async) and Alembic but no driver. `DATABASE_URL` on `main` already uses `postgresql+asyncpg://` | Nothing changes. If it is refused, another async driver is still a new dependency |
| P3 | Git author of the SAV-9 commits | The managed checkout has an empty Git name. As in SAV-4 (record 0001, P3), the commits use the name and GitHub noreply address of the earlier Developer commits | Nothing changes. Otherwise the commits are re-authored before the push, which changes their SHAs |

## Alternatives

- **A stored `tsvector` column for full-text search.** Easier to query, but it adds a column that §4 doesn't have. The expression index needs no schema change beyond the index itself.
- **The `turkish` text-search configuration.** It reduces Turkish words to their stems, so "restoranlar" would match "restoran". Saved posts mix Turkish and English, and a Turkish stemmer handles English badly. `simple` only splits and lowercases, which is predictable for both. Keyword search is the backup level L1, not the main search. SAV-16 can use prefix matching, or a later migration can change the configuration.
- **The `pgvector` and `GeoAlchemy2` packages.** They do more than we need today, and each is a new dependency that needs approval. The two types fit in one short file. If a later ticket needs more (other geometry types, other distance operators as Python methods), switching only touches `app/db/types.py`.
- **Autogenerated migrations.** Alembic's autogenerate doesn't know the vector and geography types and misses check constraints. Hand-written migrations plus the `alembic check` test give the same safety with a readable history.
- **Postgres `ENUM` types for the status columns.** A new enum value can't be used in the same transaction that adds it, which makes migrations that add a status awkward. A check constraint on a text column is changed like any other constraint.
- **Dropping the extensions on downgrade.** It could break other objects in a shared database, and the database image creates them anyway.
- **Failing instead of skipping when no test database is set.** It would make `pytest` fail on every machine without Docker, including for work that doesn't touch the database. Skipped tests are listed with their reason instead.

## Consequences

- **New setting:** `EMBEDDING_DIMENSION`, required, in `.env.example`, the README and `docker-compose.yml`. The API refuses to start without it.
- **Changing the embedding size later** needs a new migration that resizes the column, and every saved item must be embedded again. SAV-7 should pick the model before real data exists.
- **New dependencies:** `sqlalchemy[asyncio]` and `alembic` (named by the ticket) and `asyncpg` (P2).
- **`make migrate`** applies the migrations from your machine. The API image doesn't contain `alembic.ini`, so migrations don't run inside the container. A deployment ticket decides how they run in a hosted environment.
- **Not in this ticket,** left to the first ticket that needs each:
  - a shared engine and session factory for the API (SAV-11);
  - indexes for specific queries, for example items by `entity_id`, or a user's latest conversation messages;
  - timestamps on `enrichment_run`, which §4 doesn't list.
- **Keyword search has no stemming** (see Alternatives). An exact word matches in any letter case; a word with a different ending doesn't.
- **`alembic check` prints two warnings** about the `vector` and `geography` column types. They are harmless: Alembic skips comparing the types of those two columns and compares everything else.
- **Running the database tests** needs Docker, or any Postgres 16 with both extensions. A run without `TEST_DATABASE_ADMIN_URL` passes with the database tests shown as skipped, so check the summary line before trusting a green run.
