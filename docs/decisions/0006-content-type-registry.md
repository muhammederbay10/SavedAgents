# 0006: How a content type is declared

- Status: accepted for SAV-13's scope when its pull request is merged. It implements decision record 0005 (SAV-26: open categories, open values and global locations), which Muhammed accepted on 2026-11-17.
- Date: 2026-11-17
- Ticket: SAV-13

## Context

`docs/ARCHITECTURE.md` §3.4 says a built-in content type is a plug-in: an extraction schema, an optional resolver, and the filters it adds to search. It doesn't say what a declaration looks like in code. The tickets that read it come later: the pipeline (SAV-14), the resolvers (SAV-15), search (SAV-16) and the response agent (SAV-17). Their interfaces don't exist yet, so the registry has to be useful to them without guessing those interfaces.

The first version of this ticket declared closed lists: the 39 Istanbul districts, fixed cuisines, genres, streaming platforms, study subjects, exams and subtypes, and a catch-all `other` type. Muhammed declined it, because the product is for anyone, anywhere, in any language. Record 0005 replaces those lists with open values and a vocabulary that grows from real saves. This record describes the registry after that change.

## Decision

1. **A built-in type is a frozen `ContentType` object, one module per type.** `app/types/registry.py` lists them in `CONTENT_TYPES`. Adding a type is one new module and one line in the registry.
2. **The Phase 1 built-in types are `place`, `movie` (also series) and `study`.** There is no `other` type.
3. **Every extraction schema has `summary`, `category` and `tags`.** They live in the shared `Extraction` model, and each type's schema extends it. Category and tags are open text in the text model's own words.
4. **"None of these" is a normal answer.** A post without a built-in type has no type name. `CONTENT_TYPES.extraction_schema(None)` returns the shared `Extraction`, so the post keeps its summary, category and tags. The pipeline asks for the schema the same way with or without a type.
5. **No declaration lists real-world values.** There are no enums and no allowed values. Cuisine, subject, format and exam are open text in any language, and an area hint can be any place at any level in any country.
6. **Open values are marked for the vocabulary.** A schema field is marked with `Vocabulary("<field>")`, where the name is the `vocabulary.field` it belongs to (`category`, `tag`, `cuisine`, `subject`, `format`, `exam`). `vocabulary_fields(model)` returns the marks of one model. The normalization step (SAV-14) reads them, so it needs no knowledge of the types. Names, titles, topics and area hints are not marked: they are looked up by a resolver or matched as text.
7. **Schemas stay strict about shape.** Unknown keys are rejected, every key is required (optional values are `null`), text is trimmed and can't be blank, and a value must have the right shape (text, a list of text, a year in range). A reply that doesn't follow the schema fails instead of being stored half-understood.
8. **Types whose posts point at real-world things extend `EntityExtraction`** and list them in `entities`. A list post has several, and an empty list means the text named none. The pipeline can then find "the entities" of any type the same way.
9. **The resolver is declared by name** (`google_places`, `tmdb`), not as a function. The name is also the `entity.source` value in §4. A type has a resolver exactly when its schema extends `EntityExtraction`, and the declaration fails otherwise. "Does this type need an entity?" is then `content_type.resolver is not None`.
10. **A search filter declares its name, its kind and a description.** The kinds are:
    - `value`: an open value, normalized to one that exists in the user's own saves. It can name a vocabulary field, whose aliases let a question in one language find a value saved in another. `price` and `streaming` name none, because their values come from Google Places and from TMDB for the user's country.
    - `area`: a place name at any level (country, region, city, district or neighbourhood), anywhere. It replaces the `district` filter.
    - `location`, `boolean`, `integer` and `text`.
11. **`category` and `tags` are not declared as filters.** Search offers them for every item (§3.5), so a type only declares what it adds.
12. **The decision model only picks the type.** A type's `description` is its option in that choice. The registry declares no subtype or tag questions any more, because subtypes and tags are open values and a decision model can only pick from fixed options.
13. **The shared parts live in `app/types/base.py`.** The type modules import from it and the registry imports the type modules, so there is no import cycle. `base.py` is new in the §6 layout. It follows `adapters/base.py` and `models/base.py`.
14. **A test guards the boundary.** `tests/types/test_content_type_registry.py` fails when code outside `app/types/` and `app/resolvers/` compares something with a type's name.
15. **Ruff accepts the dotless `ı`.** `pyproject.toml` sets `allowed-confusables = ["ı"]`. Without it, ruff reports names such as Kadıköy in the multilingual tests as a mistyped `i`.

## Open points

- **`saved_item.subtype`.** The registry no longer produces a subtype. The open category says what a post is ("restaurant", "series"), and a resolver knows the kind of thing it found (Google's place type, TMDB's movie or TV). If a separate subtype is still wanted, it would be one more open field marked for the vocabulary. For `study`, the form of a post is the open `format` field.
- **Canonical form.** Record 0005 stores canonical values in English, lower case. The schemas accept any wording and leave the canonical form to the normalization step.

## Alternatives

- **Everything in `registry.py`, as the ticket's wording suggests.** The type modules would import the building blocks from the registry while the registry imports the type modules. That needs a registration call at import time or lazy imports. A separate `base.py` is simpler.
- **An `other` type with only category and tags.** It would make "nothing fits" look like a type, and the type decision would pick it like any other. Record 0005 rules it out.
- **Listing the fields to normalize on the `ContentType`** (for example `"entities[].cuisine"`). The paths would repeat the schema and could drift from it. A mark on the field itself can't.
- **A resolver function or interface on the type.** Its signature depends on SAV-15: what a candidate looks like, and what context a search needs (the user's country, for example). Declaring it now would be a guess with no caller. The name can be replaced by, or mapped to, the real resolver when SAV-15 defines it.
- **A Pydantic model per type for the filters.** Validation would be free, but the kind of each filter and its vocabulary field would be buried in type annotations. A plain list of filters is easier to read from the search code.
- **Checking filter values in the registry.** `search_items` has to check and normalize them against the user's own saves (SAV-16). That needs the database, so the registry only declares what kind of value a filter takes.

## Consequences

- **SAV-14 (pipeline):** builds the type choice from each type's `name` and `description` plus "none of these", and gets the schema from `CONTENT_TYPES.extraction_schema(type_or_none)`. The normalization step walks the extraction and its mentions and normalizes the fields that `vocabulary_fields` lists. The escalation applies when the type has a resolver and `entities` is empty.
- **SAV-15 (resolvers):** provides the resolvers named `google_places` and `tmdb`. Genres from TMDB go through the `genre` vocabulary field. Price levels and streaming platforms are stored as the resolvers give them, for the user's country.
- **SAV-16 (search):** reads `search_filters` to check a search's `filters`. It normalizes a `value` against the user's own values and the vocabulary, matches an `area` against every address level, and decides how each filter is queried.
- **SAV-9 (schema):** the vocabulary field names used here are `category`, `tag`, `cuisine`, `genre`, `subject`, `format` and `exam`.
- The type descriptions and field descriptions are read by models. They are new, so there is nothing to compare them with. When the evaluation set exists (SAV-19 to SAV-21), changes to them are scored on it.
