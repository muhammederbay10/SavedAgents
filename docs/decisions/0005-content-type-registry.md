# 0005: How a content type is declared

- Status: partly accepted. The choices under [Decision](#decision) are made inside SAV-13's scope and take effect when its pull request is merged. The value lists under [Pending owner decisions](#pending-owner-decisions) are first proposals and are **not approved yet**.
- Date: 2026-11-17
- Ticket: SAV-13

## Context

`docs/ARCHITECTURE.md` §3.4 says each content type is a plug-in: an extraction schema, the decision questions to ask, an optional resolver, and the filters it offers to search. It doesn't say what a declaration looks like in code. The tickets that read it come later: the pipeline (SAV-14), the resolvers (SAV-15), search (SAV-16) and the response agent (SAV-17). Their interfaces don't exist yet, so the registry has to be useful to them without guessing those interfaces.

## Decision

1. **A type is a frozen `ContentType` object, one module per type.** `app/types/registry.py` lists them in `CONTENT_TYPES`. Adding a type is one new module and one line in the registry.
2. **The shared parts live in `app/types/base.py`.** The type modules import from it and the registry imports the type modules, so there is no import cycle. `base.py` is new in the §6 layout. It follows `adapters/base.py` and `models/base.py`.
3. **Decision questions are a subtype question and tag questions.** They match the two columns they fill, `saved_item.subtype` and `saved_item.tags`. The subtype question is a choice, and each tag is one yes/no question, because those are the answers the decision model gives. `other` declares neither: its tags are free text from the text model (§3.4).
4. **Every extraction schema extends `Extraction`, which holds `summary`.** Types whose posts point at real-world things extend `EntityExtraction` and list them in `entities`. A list post has several, and an empty list means the text named none. The pipeline can then find "the entities" of any type the same way, without knowing the type.
5. **Schemas are strict.** Unknown keys are rejected, every key is required (optional values are `null`), text is trimmed and can't be blank, and values outside a closed list are rejected. A reply that doesn't follow the schema fails instead of being stored half-understood.
6. **The resolver is declared by name** (`google_places`, `tmdb`), not as a function. The name is also the `entity.source` value in §4. A type has a resolver exactly when its schema extends `EntityExtraction`, and the declaration fails otherwise. "Does this type need an entity?" is then `content_type.resolver is not None`.
7. **A search filter declares its name, its kind and a description.** The kinds are `choice`, `boolean`, `integer`, `text` and `location`. Only a `choice` filter has allowed values. They are the values that user input is normalized to (§3.5).
8. **Closed lists are `StrEnum`s in the type's module**, used both as the schema field's type and as the filter's allowed values. They are domain vocabulary, not configuration, so they aren't in `app/config.py`.
9. **`near` is declared as a `location` filter on `place`.** The `near` tool can then learn from the registry which types support distance search.
10. **For `study`, the format is the subtype.** The `format` filter allows exactly the subtype options (tip, tutorial, explainer, course).
11. **`movie` has one genre list for movies and series.** It joins TMDB's movie and TV genres. TMDB's combined TV genres ("Sci-Fi & Fantasy", "Action & Adventure", "War & Politics") map to several values. "TV Movie", "News", "Talk" and "Soap" are left out.
12. **A test guards the boundary.** `tests/test_content_type_registry.py` fails when code outside `app/types/` and `app/resolvers/` compares something with a type's name.
13. **Ruff accepts the Turkish dotless `ı`.** `pyproject.toml` sets `allowed-confusables = ["ı"]`. Without it, ruff reports every name such as Kadıköy as a mistyped `i`.

## Pending owner decisions

The lists below are in the code so the registry can be reviewed and used. Muhammed hasn't approved their contents. Changing one is an edit to a single enum or tuple.

| # | List | Proposed values |
|---|---|---|
| P1 | Place tags | breakfast, dessert, coffee, view, outdoor |
| P2 | Cuisines | turkish, kebab, seafood, meze, mediterranean, middle eastern, italian, pizza, burger, steakhouse, japanese, chinese, korean, indian, mexican, street food, vegetarian, other |
| P3 | Price levels | inexpensive, moderate, expensive, very expensive (Google Places' levels without "free") |
| P4 | Movie tags | review, clip |
| P5 | Streaming platforms | Netflix, Prime Video, Disney+, HBO Max, Apple TV+, MUBI, Exxen, Gain, Tabii, TOD, TV+, puhutv |
| P6 | Study subjects | maths, physics, chemistry, biology, turkish, english, foreign languages, history, geography, philosophy, programming, study skills, other |
| P7 | Exams | YKS, LGS, KPSS, ALES, DGS, YDS, YÖKDİL, IELTS, TOEFL |
| P8 | Study tags | practice, notes, resources |

Fixed by the design, so not pending: the place subtypes (§3.4), the 39 Istanbul districts, and the study formats (§3.4).

## Alternatives

- **Everything in `registry.py`, as the ticket's wording suggests.** The type modules would import the building blocks from the registry while the registry imports the type modules. That needs a registration call at import time or lazy imports. A separate `base.py` is simpler.
- **A resolver function or interface on the type.** Its signature depends on SAV-15: what a candidate looks like, and what context a search needs (the user's city, for example). Declaring it now would be a guess with no caller. The name can be replaced by, or mapped to, the real resolver when SAV-15 defines it.
- **A Pydantic model per type for the filters.** Validation would be free, but the allowed values that filter normalization needs would be buried in type annotations. A plain list of filters is easier to read from the search code.
- **Checking filter values in the registry.** `search_items` has to check and normalize them (SAV-16). That code doesn't exist yet, so the registry only declares what is allowed.
- **A general list of decision questions per type.** More flexible, but the data model has exactly two places for the answers (subtype and tags). A third kind of question can be added when a type needs one.
- **A `subtype` filter on every type.** "Cafés in Beşiktaş" needs it. `saved_item.subtype` is a column every type shares, and its options are already in the type's subtype question, so search can offer it for all types without a declaration per type. This is left to SAV-16.

## Consequences

- **SAV-14 (pipeline):** builds the type choice from each type's `name` and `description`, asks the type's subtype and tag questions in the same pass, and gives `extraction_schema` to the text model. The escalation applies when `resolver` is set and `entities` is empty.
- **SAV-15 (resolvers):** provides the resolvers named `google_places` and `tmdb`, and maps TMDB genres, TMDB watch providers and Google price levels to the allowed values here.
- **SAV-16 (search):** reads `search_filters` to check a search's `filters`, and normalizes user input to `allowed_values`. It decides where each filter's value is stored and how it is queried.
- The question texts and field descriptions are read by models. They are new, so there is nothing to compare them with. When the evaluation set exists (SAV-19 to SAV-21), changes to them are scored on it.
- The district list covers Istanbul only. Places in other cities are still stored, but can't be filtered by district until their districts are added.
