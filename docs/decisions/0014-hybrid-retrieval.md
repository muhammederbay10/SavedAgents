# 0014: Hybrid retrieval, filter normalization and fallback L1–L2

- Status: proposed. The decision-model wording for filter normalization waits for Muhammed's approval on SAV-16 (see [Open points](#open-points)).
- Date: 2026-11-18
- Ticket: SAV-16 (record 0012 is SAV-14's indexing pipeline, 0013 is SAV-28's)

## Context

Questions are answered from the index, never from the videos (ARCHITECTURE §3.5). SAV-16 builds that search: exact filters on what indexing stored, ranking by meaning, filter normalization ("netflx" → Netflix) and the first two fallback levels (§3.6). Record 0005 (SAV-26) sets the rules: no closed lists of values, any country and any language, filter values normalized against **the asking user's own saves** plus the gated vocabulary, and streaming providers per `(entity_id, country)`.

The pipeline (SAV-14) and the resolvers (SAV-15) are not merged yet, so this ticket reads the tables of SAV-9 as SAV-14 fills them, and seeds entities and availability rows in its tests.

## Decision

1. **One entry point.** `HybridRetriever.search(session, SearchQuery)` in `app/agent/retrieval.py` checks the query, normalizes it, runs L0 → L1 → L2 within the time budget and returns a `SearchResult`: the item ids best first, each with the level that found it, the fallback steps, the relaxed slots, how each wording was normalized, an `incomplete` flag and the availability gaps. The response agent's tools (SAV-17) call it; nothing else queries the index.
2. **Filter first, then rank.** All filters are SQL on `saved_item`. The type's filters must all hold on the **same** linked entity (one `EXISTS` over the item's entities), or on the item's own `fields`: a list post matches when one of its places or movies meets every filter, never when two titles each meet half of them. Results are ranked by cosine distance to the query text's embedding; without a text, by distance from a "near" point, or newest first. Only fully indexed items (`done`) are searched at L0 and L1.
3. **Where a filter reads its values is declared by the content type.** `SearchFilter` gets a `source` (`FilterSource`), so search never branches on a filter's name:
   - `saved_values` (default): the key named after the filter in `saved_item.fields` and in the linked `entity.data`, as one value or a list (SAV-14 already writes `fields` this way); `area` reads the entity's address levels, `near` its location;
   - `country_availability`: `entity_availability` for the asking user's country (`streaming`);
   - `user_status`: the asking user's `user_entity.status = done` (`watched`);
   - `opening_hours` (`open_now`): rejected with `UnsupportedFilterError` until the place resolver stores opening hours.
4. **The country is an argument.** The search takes the asking user's country explicitly (ISO 3166-1 alpha-2, upper-cased). A streaming filter without one raises `CountryRequiredError`, and the agent asks the user once (SAV-17/18). It is never guessed from the language or a location.
5. **Availability per country, never substituted.** Streaming filters join `entity_availability` on `(entity_id, country)` only. For the titles the query covers (with a streaming filter: every title that meets the other filters, plus every title L2 returned), a missing row, or one older than `AVAILABILITY_MAX_AGE_DAYS`, is returned as an `AvailabilityGap(entity_id, country, missing | stale)`: the signal SAV-15 acts on to fetch or refresh exactly that row. A stale row is still used until it is refreshed; another country's row never is.
6. **Filter normalization** (`app/agent/filter_normalization.py`) runs on the collection, the category, each tag and each `value` or `area` filter:
   - **Exact hit, no model call:** the wording, trimmed and lower-cased, equals one of the user's own values, or a canonical value or *verified* alias of its vocabulary field. Candidate and rejected aliases never count.
   - **Options:** otherwise the user's own values for that slot, ranked by meaning (the vocabulary embeddings of the values the user has) and by spelling, taken in turn, at most `FILTER_NORMALIZATION_MAX_OPTIONS`. Values the wording was rejected as an alias of are left out, compared trimmed and lower-cased on both sides, so `Restaurant` is left out too. If the database fails the ranking by meaning, it runs in a savepoint, is logged without the wording, and the options come from spelling alone.
   - **Decision:** Gemini Decision 1 picks one option or "none of these". A pick below `FILTER_NORMALIZATION_MIN_PROBABILITY`, "none" or a model error keeps the wording as written; the filter then finds nothing and L2 relaxes it.
   - The user's values are read only through their own items. Shared tables are read only for public data (entities, availability) or gated generic wording (vocabulary). Normalization never writes to the vocabulary.
7. **L1** runs when the query text can't be embedded or the database fails the vector query (inside a savepoint, so the session stays usable). It keeps every filter and adds Postgres full-text search, any word of the text, on the indexed expression `saved_item_search_vector()` (caption, summary, transcript), ranked by `ts_rank`.
8. **L2** runs when L0 or L1 found fewer than `SEARCH_MIN_RESULTS` items. It keeps the user, collection, type, category, platform, date, a near point and, after L1, the keyword match on the text; widens a district or neighbourhood to its city (per country); drops tags and the type's other filters; and lists those slots in `relaxed`. It then searches items that are not fully indexed (`pending`, `enriching`, `needs_user`, `failed`) by any word of the query text, the wordings and their normalized values. Results found at L2 come after the exact ones.
9. **Logging and the time budget.** Every step up is logged once as `search_fallback` with the user id, level, reason and counts, never the query's text. The whole search, model calls included, runs inside `SEARCH_TIME_BUDGET_SECONDS`; when it runs out, the result keeps what was found and is marked `incomplete`.
10. **Prompt wording lives in `app/agent/prompts.py`** as a versioned `RetrievalPrompts`, like the pipeline's `PipelinePrompts`. The approved wording is added as `RETRIEVAL_PROMPTS` once Muhammed approves it; until then only tests construct one.

## Alternatives considered

- **Branching on filter names** (`if name == "streaming"`) in search: simpler, but it is the type logic outside `app/types/` that AGENTS.md forbids, and every new type would edit search.
- **Normalizing against the whole vocabulary** instead of the user's own values: a value nobody in this user's saves has can never match, and a typo could be mapped to another user's wording. The vocabulary is still used for exact hits and for ranking by meaning.
- **A trigram index (`pg_trgm`) for spelling similarity:** a new database extension and migration. The user's own values for one filter are few, so ranking them in Python (`difflib`) is fast enough.
- **A cosine-distance cut-off for "few results":** would need a threshold tuned on the evaluation set (SAV-19/20), which doesn't exist yet. Without one, vector ranking orders the filtered items, and L2 is triggered by the filters.
- **Copying SAV-14's vocabulary matcher:** SAV-14 isn't merged and its matcher writes (candidate aliases, new values). Search only needs read-only lookups, kept in `app/agent/saved_values.py`.

## Consequences

- **SAV-15 (resolvers)** stores resolver values under the filter's name in `entity.data` (`genre` as a list, `year` as a number, `price`), and providers as `{"<offer kind>": ["<provider name>", …]}` per `(entity_id, country)`, the shape SAV-9's tests use. It fetches or refreshes the rows named by `availability_gaps`.
- **SAV-14** and this ticket compare wordings the same way (trimmed, white space collapsed, lower-cased). Once SAV-14 is merged, `saved_values.comparison_form`, `vocabulary_exact_hit` and `rejected_values` can call its `canonical_form`, `find_exact` and `rejected_value_ids` instead; whichever branch merges second makes that change.
- **SAV-17 (agent)** builds `SearchQuery` from the tool arguments, handles `CountryRequiredError` by asking once, tells the user when results were relaxed or `incomplete`, and passes availability gaps to SAV-15.
- Python lower-cases the Turkish `İ` to `i̇`, so "BEŞİKTAŞ" is not an exact hit for "Beşiktaş"; it goes to the decision model, which picks it.
- Ranking by vector over the filtered items is exact (no approximate index) at Phase 1 sizes. The test seeds 600 items and checks a typical filtered vector search stays under 200 ms locally, model calls excluded.

## Open points

1. **Prompt wording** for the normalization decision, posted on SAV-16 for Muhammed's approval.
2. **Threshold values** (`FILTER_NORMALIZATION_MIN_PROBABILITY=0.6`, `SEARCH_MIN_RESULTS=3`, `AVAILABILITY_MAX_AGE_DAYS=7`) are first guesses, to be scored on the evaluation set (SAV-19/20).
3. **`open_now`** waits for opening hours from SAV-15.
