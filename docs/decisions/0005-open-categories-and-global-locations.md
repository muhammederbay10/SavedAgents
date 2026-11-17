# 0005: Open categories, open values and global locations

- Status: accepted by Muhammed in chat on 2026-11-17. The review corrections (privacy gate, corrections, per-country streaming, exact-hit rule) were approved by him in chat the same day.
- Date: 2026-11-17
- Ticket: SAV-26

## Context

SavedAgent is for **everyone, anywhere, in any language**. The first version of `docs/ARCHITECTURE.md` was shaped by the first tester's own saves in Istanbul. It used Istanbul's *ilçe* and *mahalle*, Turkish exams (YKS, YDS), "where to stream in Turkey", and a fixed list of content types. SAV-13 followed it faithfully: the 39 Istanbul districts as the only allowed locations, and closed lists of cuisines, genres, streaming platforms, study subjects and exams.

Reviewing SAV-13, Muhammed asked: what happens when a user saves something that isn't on our lists, or lives somewhere else? With closed lists, the item is forced into the nearest wrong bucket or dumped into "other", and filters can't express it.

## Decision

1. **Every item gets an open category and tags.** The text model names them in plain words ("restaurant", "car review", "skincare routine"). They're searchable at once, and the confirmation DM tells the user how the item was saved, so they can correct it.
2. **Built-in types only where we add behaviour.** Place, movie/series, book and later others exist because they have a resolver, extra fields or special filters. Gemini Decision 1 picks one of them **or "none of these"**. "None" is a normal outcome, not a failure: the item keeps its open category, and there is no `other` type. A category that keeps coming up can be promoted to a built-in type as config.
3. **No value lists in code. A shared vocabulary grows from real saves.** Category, tags, cuisine, genre, subject, exam, format and subtype are open text. Each new wording goes through these steps:
   - **Exact hit:** if it equals a canonical value or a *verified* alias of the same field, that value is reused with no model call. This is the only shortcut.
   - **Privacy gate** (point 4) for anything else.
   - **Match or new:** embedding search finds candidates, then Gemini Decision 1 picks one or "new". A nearby vector alone is never a match. A match stores the wording as a *candidate* alias; "new" adds a canonical value (English, lower case).
   - A candidate alias becomes *verified* after confident matches for several different users, or after eval review.
   The item always keeps its own wording in `raw_values`.
4. **Only generic words enter the shared vocabulary.** The vocabulary has no link to any user, so a privacy gate runs before any value or alias is added: code checks (handles, URLs, emails, phone-like numbers, length), then a Gemini Decision 1 yes/no that only passes generic wording with nothing about a private person or the user themselves. Uncertain counts as no. Rejected wording stays on the user's own item and is deleted with "delete my data".
5. **A correction moves one item; it never creates a synonym.** "No, it's a travel video" changes that item only. The old and new values are not linked, and no other item changes. Corrections count against the alias that caused the mistake, so it isn't verified, or is flagged if it already was. A wrong alias is marked `rejected`, and only the items matched through it are re-normalized from their own `raw_values`. Values a user set by hand are kept.
6. **Places are global.** Location fields come from Google Places' address components for any country: country, region or state, city, district, neighbourhood, plus coordinates. Any of them may be empty. Area filters match any level.
7. **Filters normalize against the user's own data.** "beşilktaş" → Beşiktaş and "bruklin" → Brooklyn are matched against the areas and values that exist in this user's saves (plus the vocabulary), never against a fixed list.
8. **Streaming availability is per movie and per country.** TMDB watch providers are stored in `ENTITY_AVAILABILITY` with key `(entity_id, country)` and their own `refreshed_at`. Refreshing one country never changes another. Search uses the asking user's country (from their profile, or asked once).
9. **Any language.** Captions, speech, extraction and replies work in any language. Examples in the docs may use Istanbul or Turkish because they come from real test saves, but nothing may depend on them.

## Alternatives considered

- **Closed lists per country** (Istanbul districts, Turkish platforms…): doesn't scale and fails for every user outside the list.
- **Fully free text with no vocabulary:** "korean", "Korean food" and "Kore mutfağı" would become three values, and filters would miss items.
- **Only vector search, no categories:** works for fuzzy questions but can't give exact filters ("only my car videos") or tell the user how something was saved.
- **A per-user vocabulary instead of a shared one:** no privacy gate needed, but every user starts empty and pays a decision call for every common word. The gate keeps the shared table safe instead.
- **Learning synonyms from user corrections:** a correction usually means the item was misclassified, not that two values mean the same, so it would merge unrelated values for everyone.
- **Streaming providers inside `ENTITY.data`:** one shared row per movie can only hold one country's answer, so users in different countries would overwrite each other.

## Consequences

- **SAV-13 (content types):**
  - remove `IstanbulDistrict` and every closed value enum;
  - add the open `category` and tags to every type's schema;
  - make built-in types optional ("none of these"), with no `other` type;
  - turn value fields into open text that goes through the vocabulary;
  - turn the location filter into an `area` that can match any address level.
- **SAV-9 (schema):**
  - add `saved_item.category` and `saved_item.raw_values`;
  - add `entity.country`, `region` and `neighbourhood` next to `city` and `district`;
  - add `user.country` and `user.home_area`;
  - add the `vocabulary` table (field, value, embedding, item count) with a unique `(field, value)`, and a `vocabulary_alias` table (alias, status `candidate | verified | rejected`, probability, correction count) instead of an aliases array;
  - add `entity_availability` with primary key `(entity_id, country)`, providers and `refreshed_at`;
  - "delete my data" removes `raw_values` and enrichment runs with the items and lowers `item_count`.
- **SAV-14 (pipeline):** a normalization step after extraction: exact hit, privacy gate, match or new. It records which alias each value matched through in the item's `ENRICHMENT_RUN`.
- **SAV-15 (resolvers):** maps the full Google Places address, and fetches TMDB providers per country into `entity_availability`.
- **SAV-16 (search):** normalizes against the user's own values and areas, and filters streaming through `entity_availability` for the user's country.
- **SAV-17 (agent):** filters on `category` and `tags`, phrases replies in the user's language, and `update_item` changes only the corrected item, never the vocabulary's synonyms.
- **Limits:**
  - An exact hit on a canonical value or verified alias costs nothing. Every other new wording costs an embedding lookup, one privacy-gate decision and one match-or-new decision. Most values hit exactly once the vocabulary has grown.
  - A candidate alias can be wrong until it's corrected or reviewed. We log matches with their probability and review low-confidence ones in evals.
  - The privacy gate will sometimes reject harmless wording. That only costs consistency for that item, which stays searchable by its owner.
  - The vocabulary starts empty, so the first saves create most values and pay the full cost.
  - The canonical English form is a convention for matching. Replies show values in the user's language.
  - Thresholds (`VOCAB_ALIAS_MIN_PROBABILITY`, `VOCAB_ALIAS_MIN_USERS`, `VOCAB_PRIVACY_MIN_PROBABILITY`, `VOCAB_MAX_VALUE_LENGTH`, `AVAILABILITY_MAX_AGE_DAYS`) live in config and are tuned on the eval set.
