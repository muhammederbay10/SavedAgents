# 0005: Open categories, open values and global locations

- Status: accepted by Muhammed in chat on 2026-11-17.
- Date: 2026-11-17
- Ticket: SAV-26

## Context

SavedAgent is for **everyone, anywhere, in any language**. The first version of `docs/ARCHITECTURE.md` was shaped by the first tester's own saves in Istanbul. It used Istanbul's *ilçe* and *mahalle*, Turkish exams (YKS, YDS), "where to stream in Turkey", and a fixed list of content types. SAV-13 followed it faithfully: the 39 Istanbul districts as the only allowed locations, and closed lists of cuisines, genres, streaming platforms, study subjects and exams.

Reviewing SAV-13, Muhammed asked: what happens when a user saves something that isn't on our lists, or lives somewhere else? With closed lists, the item is forced into the nearest wrong bucket or dumped into "other", and filters can't express it.

## Decision

1. **Every item gets an open category and tags.** The text model names them in plain words ("restaurant", "car review", "skincare routine"). They're searchable at once, and the confirmation DM tells the user how the item was saved, so they can correct it.
2. **Built-in types only where we add behaviour.** Place, movie/series, book and later others exist because they have a resolver, extra fields or special filters. Gemini Decision 1 picks one of them **or "none of these"**. "None" is a normal outcome, not a failure: the item keeps its open category. A category that keeps coming up can be promoted to a built-in type as config.
3. **No value lists in code. A shared vocabulary grows from real saves.** Category, tags, cuisine, genre, subject, exam, format and subtype are open text. Each new value is matched against existing values of the same field: embedding search finds candidates, then Gemini Decision 1 picks one or "new". A match reuses the canonical value and keeps the new wording as an alias; "new" adds a value. Canonical values are English, lower case; aliases can be in any language. User corrections update the vocabulary.
4. **Places are global.** Location fields come from Google Places' address components for any country: country, region or state, city, district, neighbourhood, plus coordinates. Any of them may be empty. Area filters match any level.
5. **Filters normalize against the user's own data.** "beşilktaş" → Beşiktaş and "bruklin" → Brooklyn are matched against the areas and values that exist in this user's saves (plus the vocabulary), never against a fixed list.
6. **Streaming platforms come from TMDB** watch providers for the user's country (from their profile, or asked once).
7. **Any language.** Captions, speech, extraction and replies work in any language. Examples in the docs may use Istanbul or Turkish because they come from real test saves, but nothing may depend on them.

## Alternatives considered

- **Closed lists per country** (Istanbul districts, Turkish platforms…): doesn't scale and fails for every user outside the list.
- **Fully free text with no vocabulary:** "korean", "Korean food" and "Kore mutfağı" would become three values, and filters would miss items.
- **Only vector search, no categories:** works for fuzzy questions but can't give exact filters ("only my car videos") or tell the user how something was saved.

## Consequences

- **SAV-13 (content types):**
  - remove `IstanbulDistrict` and every closed value enum;
  - add the open `category` and tags to every type's schema;
  - make built-in types optional ("none of these");
  - turn value fields into open text that goes through the vocabulary;
  - turn the location filter into an `area` that can match any address level.
- **SAV-9 (schema):**
  - add `saved_item.category`;
  - add `entity.country`, `region` and `neighbourhood` next to `city` and `district`;
  - add `user.country` and `user.home_area`;
  - add the `vocabulary` table (field, value, aliases, embedding, item count) with a unique `(field, value)`.
- **SAV-14 (pipeline):** a normalization step after extraction.
- **SAV-16 (search):** normalizes against the user's own values and areas.
- **SAV-17 (agent):** filters on `category` and `tags`, and phrases replies in the user's language.
- **SAV-15 (resolvers):** maps the full Google Places address and the TMDB providers for the user's country.
- **Limits:**
  - Normalization adds one decision call for each new value. Values already in the vocabulary cost an embedding lookup only.
  - A wrong match merges two different values until a user corrects it. We log matches with their probability and review low-confidence ones in evals.
  - The vocabulary starts empty, so the first saves create most values.
  - The canonical English form is a convention for matching. Replies show values in the user's language.
