# 0007: Database schema and migrations

- Status: accepted. Muhammed approved the owner decisions under [Owner decisions](#owner-decisions) in chat on 2026-11-17 (recorded on SAV-9). The other choices under [Decision](#decision) are made inside SAV-9's scope and take effect when its pull request is merged.
- Date: 2026-11-17
- Ticket: SAV-9 (the number 0005 went to SAV-26's record on open categories, and 0006 to SAV-13's)

## Context

`docs/ARCHITECTURE.md` §4 lists the tables and their columns. It doesn't say which columns may be empty, what happens to rows when a user is deleted, or how each index is built. SAV-9 creates the tables, so it has to settle these. Three other records add to the schema:

- **0003:** a reel saved into a collaborative collection arrives with a video file but no post id, no caption and no post link.
- **0004:** every item keeps the id of the message it came in with (`source_message_id`), so answers can reply to it.
- **0005 (SAV-26):** every item gets an open category, places are global, and open values are kept consistent through a shared vocabulary.

## Decision

1. **One migration creates the nine Phase 1 tables:** `user`, `platform_account`, `saved_item`, `entity`, `item_entity`, `user_entity`, `enrichment_run`, `conversation_msg` and `vocabulary`. `memory` comes in Phase 2. `user` is a reserved word in Postgres, so raw SQL must write it as `"user"`. SQLAlchemy does that by itself.
2. **Migrations are written by hand, and a test compares them with the models.** `app/db/models.py` describes the tables for queries. `tests/test_migrations.py` runs `alembic check`, which fails when the models and a migrated database differ.
3. **The embedding size is read from `EMBEDDING_DIMENSION` when the first migration runs.** It sizes both `saved_item.embedding` and `vocabulary.embedding`. The models leave the size out, and the database enforces it. The setting accepts 1 to 2000, because pgvector can't build an HNSW index on a larger `vector` column. Which embedding model to use, and so the final dimension, is decided in SAV-7.
4. **Which columns are required.**
   - Required: every id and owner reference, `platform`, an entity's `type`, `source`, `external_id` and `name`, both `status` columns, the role and content of a conversation message, and a vocabulary row's `field` and `value`.
   - Filled by the database when left out: ids (`gen_random_uuid()`), `created_at`, `saved_at`, `refreshed_at` and `at` (now), `saved_item.status` (`pending`), `tags` and `aliases` (empty list), `fields`, `attributes` and `data` (empty object), `item_entity.position` and `vocabulary.item_count` (0).
   - Everything else may be empty, because indexing fills it in later or not at all. That includes `platform_post_id` and `post_url` (see [Dedupe keys](#dedupe-keys-on-saved_item)), `type` and `category`, both embeddings, and every address field of an entity.
5. **The two documented status lists are enforced by the database.** `saved_item.status` must be `pending`, `enriching`, `needs_user`, `done` or `failed`. `user_entity.status` must be `want` or `done`. These are states of our own pipeline, not real-world values. Every real-world value stays open text with no list behind it, as record 0005 requires: `platform`, `media_type`, `type`, `category`, `subtype`, `tags`, the address fields, `country`, `language`, and the vocabulary's `field` and `value`.
6. **Deleting a `user` row deletes all of that user's data:** platform accounts, saved items (with `source_message_id`, entity links and enrichment runs), entity statuses and conversation messages. Two tables are shared by all users and stay:
   - `entity` rows describe public things, such as a restaurant or a movie.
   - `vocabulary` rows are generic words, such as "korean" or "car review". The table has no reference to a user, so nothing personal can be reached from it.
7. **Indexes.**
   - The ticket's list: unique `(user_id, platform, platform_post_id)` on `saved_item`, unique `(source, external_id)` on `entity`, GIST on `entity.location`, GIN on `entity.data` and `saved_item.fields`, HNSW on `saved_item.embedding`.
   - From this record's dedupe decision: unique `(user_id, source_message_id)` on `saved_item`, for rows that have a message id. It also serves the reply lookup from record 0004.
   - From record 0005: unique `(field, value)` on `vocabulary`.
   - **One addition:** unique `(platform, platform_user_id)` on `platform_account`. Without it, two users could claim the same Instagram account, and the webhook couldn't tell whose message arrived.
   - The HNSW index uses cosine distance (`vector_cosine_ops`, queried with `<=>`).
8. **Full-text search is an index on an expression, not a new column.** The index covers `caption`, `summary` and `transcript` together, with Postgres's `simple` text-search configuration, which works the same for every language. Queries must use the same expression, so `saved_item_search_vector()` in `app/db/models.py` builds it, and a test checks that Postgres can use the index for it.
9. **The pgvector and PostGIS columns use two small types of our own** (`Vector` and `GeographyPoint` in `app/db/types.py`) instead of the `pgvector` and `GeoAlchemy2` packages. They send and read values as text, which the driver handles without setup. A location is a `GeoPoint(latitude, longitude)` in Python.
10. **`downgrade` leaves the PostGIS and pgvector extensions in place.** The database image enables them too, so the migration can't know that it created them.
11. **The first migration was edited in place for the SAV-26 additions and the dedupe keys**, instead of adding a second one. It had not been pushed or applied to any database that holds data. From the merge on, a schema change is always a new revision.

### Dedupe keys on `saved_item`

A reel saved into a collaborative collection has no post id and no post link (record 0003), so the key `(user_id, platform, platform_post_id)` can't dedupe it. Approved by Muhammed:

| Rule | In the database |
|---|---|
| A post is stored once per user | Unique `(user_id, platform, platform_post_id)`, as designed. Postgres treats two missing post ids as different, so items without one never collide here |
| A message is stored once per user | Unique `(user_id, source_message_id)` for rows that have a message id. It applies to every item with a message id, not only collection saves |
| Every item can be shown back to the user | A check: a post id, a message id or a post link must be filled. An item with only a link (a later import) is allowed |
| A missing value is stored as missing | Three checks: `platform_post_id`, `source_message_id` and `post_url` are never empty text. An empty text would count as a real value in the unique keys, so two items without an id would collide |

`post_url` and `platform_post_id` may both be empty. Besides collection saves, a photo post shared by DM has no public link either: its `url` is a signed image link (record 0002, finding 7).

The check on `post_url` goes one step past what was approved, which named the two id columns. Without it, an empty link would satisfy the "post id, message id or post link" rule while showing the user nothing.

**The accepted limit: this dedupes messages, not reels.**

| Case | Result |
|---|---|
| Meta delivers the same webhook twice | One item: same message id |
| The same reel or post is shared by DM twice | One item: same post id |
| The same reel is saved into the collection twice, or into two shared collections | **Two items.** Each save is a new message, and the payload holds nothing else that identifies the reel |
| The same reel is saved into the collection and also shared by DM | **Two items:** one with caption and link, one with only the video. They share no id |

For the last two cases a user can see the same reel twice in an answer. For places and movies both items point to the same `entity`, so search (SAV-16) can show one result per entity. A real fix needs a fingerprint of the video itself, for example the `asset_id` in the CDN link or a hash of the file. Nobody has tested whether two saves of one reel give the same value, so no fingerprint column exists yet. That spike is for later, not this ticket.

Two more limits:
- The message-id key assumes one saved item per message. Every recorded share and collection save has exactly one attachment. If Meta ever sends two shares in one message, the adapter (SAV-11) has to handle it.
- The message id is an opaque string (164 characters in our recordings). It is stored as text and never parsed.

### Open categories, global places and the vocabulary (record 0005)

- `saved_item.category` is the open category every item gets. `type` stays for the built-in type and is empty when none applies.
- `entity` has five address levels: `country`, `region`, `city`, `district` and `neighbourhood`. Any of them may be empty, and none has a list of allowed values.
- `user.home_area` replaces `home_city`, and `user.country` is new. Both are open text.
- `vocabulary` has `field`, `value`, `aliases`, `embedding`, `item_count` and `created_at`, with one row per `(field, value)`.
- The schema stores what it is given. That canonical values are English and lower case, and how a country is written (a name or a code), are conventions of the code that writes the rows (SAV-14, SAV-15), not rules in the database.

## Owner decisions

Muhammed approved these on SAV-9 on 2026-11-17.

| # | Decision | Approved |
|---|---|---|
| P1 | How to dedupe collection-saved reels | The keys and checks under [Dedupe keys](#dedupe-keys-on-saved_item), with the "messages, not reels" limit accepted and the fingerprint spike left for later |
| P2 | Runtime dependency `asyncpg` | Yes. It is the Postgres driver for SQLAlchemy's async engine. The `pgvector` and `GeoAlchemy2` packages are not added |
| P3 | Git author of the SAV-9 commits | The same name and GitHub noreply address as the SAV-4 commits (record 0001, P3) |
| P4 | Database tests without a test database | They are skipped, not failed. Pytest lists every skipped test with its reason (`-ra`) |

## Alternatives

- **Writing the message id into `platform_post_id`** to keep a single key. It would put a message id in a column that other code treats as a real post id.
- **One unique index over "post id, or else message id".** It mixes two kinds of id in one key and makes the upsert in SAV-11 harder to read.
- **Requiring a post id or a message id on every row.** It would reject a later import that only has a link.
- **A stored `tsvector` column for full-text search.** Easier to query, but it adds a column that §4 doesn't have. The expression index needs no schema change beyond the index itself.
- **A language-specific text-search configuration** such as `turkish` or `english`. It reduces words to their stems, so "restaurants" would match "restaurant". But saved posts can be in any language (record 0005), and a stemmer for one language handles the others badly. `simple` only splits and lowercases, which is predictable everywhere. Keyword search is the backup level L1, not the main search, and SAV-16 can use prefix matching.
- **The `pgvector` and `GeoAlchemy2` packages.** They do more than we need today, and each is a new dependency. The two types fit in one short file. If a later ticket needs more (other geometry types, other distance operators as Python methods), switching only touches `app/db/types.py`.
- **Autogenerated migrations.** Alembic's autogenerate doesn't know the vector and geography types and misses check constraints. Hand-written migrations plus the `alembic check` test give the same safety with a readable history.
- **Postgres `ENUM` types for the status columns.** A new enum value can't be used in the same transaction that adds it, which makes migrations that add a status awkward. A check constraint on a text column is changed like any other constraint.
- **Dropping the extensions on downgrade.** It could break other objects in a shared database, and the database image creates them anyway.
- **Failing instead of skipping when no test database is set.** It would make `pytest` fail on every machine without Docker, including for work that doesn't touch the database.

## Consequences

- **New setting:** `EMBEDDING_DIMENSION`, required, in `.env.example`, the README and `docker-compose.yml`. The API refuses to start without it.
- **Changing the embedding size later** needs a new migration that resizes both embedding columns, and every saved item and vocabulary value must be embedded again. SAV-7 should pick the model before real data exists.
- **New dependencies:** `sqlalchemy[asyncio]` and `alembic` (named by the ticket) and `asyncpg` (P2).
- **`make migrate`** applies the migrations from your machine. The API image doesn't contain `alembic.ini`, so migrations don't run inside the container. A deployment ticket decides how they run in a hosted environment.
- **SAV-11 (ingestion):** `CaptureEvent.post_url` may be empty. Store a missing id or link as `None`, never as empty text. The same message can't be stored twice, so a repeated webhook can be detected from the unique key.
- **Not in this ticket,** left to the first ticket that needs each:
  - a shared engine and session factory for the API (SAV-11);
  - indexes for specific queries, for example items by `entity_id`, a user's latest conversation messages, the nearest vocabulary values by embedding, or a vocabulary row by alias. The vocabulary is small and always searched within one field, so it starts without them;
  - keeping `vocabulary.item_count` up to date and cleaning up unused values (SAV-14);
  - timestamps on `enrichment_run`, which §4 doesn't list.
- **Keyword search has no stemming** (see Alternatives). An exact word matches in any letter case; a word with a different ending doesn't.
- **`alembic check` prints warnings** about the `vector` and `geography` column types. They are harmless: Alembic skips comparing the types of those columns and compares everything else.
- **Running the database tests** needs Docker, or any Postgres 16 with both extensions. A run without `TEST_DATABASE_ADMIN_URL` passes with the database tests shown as skipped, so check the summary line before trusting a green run.
