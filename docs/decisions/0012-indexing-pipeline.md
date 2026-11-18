# 0012: The indexing pipeline: steps, speech routing, vocabulary and prompts

- Status: proposed with SAV-14. It takes effect when SAV-14's pull request is merged. Muhammed approved the points under [Owner decisions](#owner-decisions) on SAV-14 on 2026-11-18.
- Date: 2026-11-18
- Ticket: SAV-14

## Context

`docs/ARCHITECTURE.md` §3.3 draws the pipeline, and §3.4 the vocabulary. Records 0005 to 0011 settle the parts it is built from: open categories and the vocabulary (0005), the type registry (0006), the schema and the counting rule for "delete my data" (0007), the models and the speech rule (0008), the job contract (0009), the model clients (0010) and the DM templates (0011). Several things were still open: where the prompts live, what "nearly empty" means, how a job resumes, what a reel shared by DM can be indexed from, and in which language the confirmation goes out before the user's language is known.

## Decision

### Steps and resuming

1. **One arq job per item**, `index_saved_item(user_id, saved_item_id)` as record 0009 defines it. `python -m app.pipeline.worker` runs it. A worker runs `INDEXING_CONCURRENCY` jobs at once, so a bulk import fans out over every free slot of every worker.
2. **Steps in order:** fetch, classify, transcribe, extract (with the escalation), normalize, embed, then the item is `done` and the confirmation is sent (`confirm`). Resolving entities (SAV-15) goes between normalize and embed.
3. **Each step's result is a row in `enrichment_run`**, one per step, written in the same transaction as the step's changes to the item. A job that runs again reads the finished steps and continues after them. No model is asked twice for a finished step, and the media is not downloaded twice.
4. **Failures.** A model or network error, ffmpeg under load, or a briefly unreachable database or Redis is recorded on the step and the job runs again after `INDEXING_RETRY_DELAY_SECONDS` times the try's number, up to `INDEXING_MAX_TRIES` runs. The last run marks the item `failed`. Any other error is a bug: the item is marked `failed` at once and the error is raised. An item with no media and no caption fails, because there is nothing to understand.
5. **Status:** `pending → enriching → done | failed`. `needs_user` comes with SAV-15's "which one?" question.
6. **The job's arguments are ids and its result is the outcome's name**, so arq keeps no content. Step outputs can hold the post's content (the extraction, transcript timestamps). They belong to the item and are deleted with it.
7. **"Delete my data" during a job.** Every write first locks the user's row, as ingestion does (record 0009, point 13), and stops if the user or the item is gone. Media that the fetch step stored for a user who was deleted in the meantime is erased again.

### Media

8. **Only the link the platform sent is downloaded** (`CaptureEvent.media_url`), never a post's page. A link that is refused, a file over `MEDIA_MAX_DOWNLOAD_BYTES`, or a file that is not video or image media makes the media *unavailable*, and the item is indexed from its text. A timeout, rate limit or server error is tried again.
9. **Reels shared by DM are indexed from their caption only** (owner decision Q1). Meta sends no file for them (record 0002), so they get no speech-to-text and no escalation. Reels saved into a collaborative collection and `ig_post` images have a file link and get the full pipeline.
10. **What is kept:** a video becomes an MP4 at most `MEDIA_STORED_HEIGHT_PIXELS` high, with its audio track, plus a still picture; an image becomes a JPEG of that height. The decision model and the escalation see these copies.
11. **Object storage is a local folder in development** (`MEDIA_STORAGE_DIR`) behind a small `MediaStore` interface: `<folder>/<user id>/<saved item id>/<file>`. `LocalMediaStore.erase_user` removes a user's folder and has the shape of the eraser `delete_user_data` takes. A hosted store comes with deployment.
12. **ffmpeg and ffprobe run as programs** (owner decision D1). No Python package is added. Their error output is never logged, because it can name the file.

### Speech

13. **The audio track decides, then the transcript** (record 0008). ffprobe reads the downloaded file. No audio track: no speech-to-text. An audio track: speech-to-text always runs, the default model first and the fallback on an error or an empty answer, also when the caption already names the thing. The decision model is not asked whether a post has speech.
14. **The near-empty rule.** A final transcript has speech when it has at least `TRANSCRIPT_MIN_SPEECH_CHARACTERS` letters, digits or combining marks, in any script (Unicode categories L, N and M). Spaces, punctuation and symbols such as "♪" don't count. Words are not counted, because many languages don't separate them with spaces. `.env.example` sets 4.
15. **A failure is not silence** (owner decision Q3). If no model produced a transcript, or the default model heard no words and the fallback then failed, the step fails and the job runs again. Only an empty or near-empty transcript from a call that succeeded means no speech.
16. The transcript's language and segment and word timestamps are kept in the step's output. They are `null` when the answering model did not return them, as with the fallback model.

### Extraction and the escalation

17. The text model gets the collection hint, the caption and the transcript, and the schema the registry gives for the type, or the shared schema for an item with no built-in type. A reply that breaks the schema fails the step; the error names where, never what.
18. **The escalation** runs when the type has a resolver and the text named no entity. `google/gemini-2.5-flash` gets the stored video or image with the same schema. Its answer is used when it names an entity.
19. **Logs** hold, per indexed item, the type, whether it was escalated, and the cost of its model calls, with the number of calls that reported no charge. The escalation rate and the cost per item are read from them.

### Normalization

20. Every schema field marked `Vocabulary(...)` is an open value with a path (`category`, `tags.0`, `entities.1.cuisine`). The item keeps each value's own wording in `raw_values`, by path: `{"field", "wording", "user_set"}`.
21. The steps of §3.4 run in order: exact hit on a canonical value or a **verified** alias (no model call), then the privacy gate, then match-or-new among the `VOCAB_MATCH_CANDIDATES` nearest values of the same field. Candidate and rejected aliases are never a shortcut, and a value the wording was rejected as an alias of is never offered again.
22. **The privacy gate's code checks** read characters, not words of a language: an `@` before a letter or digit (handles and email addresses), a link (a scheme, `www.`, or a domain with a path), a run of `VOCAB_PHONE_MIN_DIGITS` digits of any script with separators, or more than `VOCAB_MAX_VALUE_LENGTH` characters. Then the decision model must say "generic" with at least `VOCAB_PRIVACY_MIN_PROBABILITY`. Wording that fails stays on the item as written, searchable by its owner only, and is deleted with it. Wording is never logged.
23. **Canonical form:** trimmed, runs of white space made one space, lower case. The extraction prompt asks for open values in English, lower case (owner decision P2), so a new canonical value is the wording in that form, with no translation call.
24. **Provenance:** the normalize step's output lists, by path, the field, the outcome (`exact_value`, `exact_alias`, `matched`, `new`, `private` or `undecided`), the value and alias ids, and the match probability.
25. **Verification:** after an item's normalize step is committed, each alias it matched with at least `VOCAB_ALIAS_MIN_PROBABILITY` is verified once matches through it come from `VOCAB_ALIAS_MIN_USERS` different users, counted from the normalize steps.
26. **Rejecting an alias** (`AliasRejection.reject`) marks it `rejected`, then normalizes again, from `raw_values`, only the values that went through it, on only the items whose normalize step names it. Values marked `user_set` stay. Corrections from users (SAV-17) are not synonym evidence and don't come through here.
27. **Counts move with values.** When an item starts or stops using a value, its `item_count` goes up or down by one, by the same rule `delete_user_data` lowers it with (record 0007). Open values inside nested objects, such as each place's cuisine, are also listed once at the top of `saved_item.fields` under their vocabulary field, because that is where the rule finds them.

### Prompts and the confirmation

28. **All wording sent to models is in `app/pipeline/prompts.py`** (owner decision P1), in one frozen `PipelinePrompts` object with a `version`, now `2026-11-18.1`. The version is stored in the outputs of classify, extract and normalize, and logged with each indexed item. Templates take the post's text only as values of named placeholders. A wording change bumps the version and is scored on the eval set.
29. **The confirmation** says how the item was saved: "Saved as {category}". It is written in the user's language when their profile has one with wording. Otherwise it is written in English (owner decision Q2), and the step's output says why. A failed send is recorded and not tried again, because a message whose delivery is unknown would be sent twice.
30. **The HTTP client's request lines are no longer logged below WARNING.** httpx logs every request's full URL at INFO, and a media link is a signed, private URL.

## Owner decisions

Muhammed decided these on SAV-14 on 2026-11-18.

| # | Question | Decision |
|---|---|---|
| P1 | Where prompts live and how they are versioned | One frozen, versioned `PipelinePrompts` in `app/pipeline/prompts.py`; the version is stored and logged per step |
| P2 | The wording of the five prompts and the canonical form | Approved as proposed. Open values are written in English, lower case; no translation call |
| D1 | ffmpeg and ffprobe | Installed as programs in the worker image and on developers' machines; no Python package |
| Q1 | Reels shared by DM have no file | Indexed from the caption only; nothing is read from the reel's page. A separate spike checks for an official route |
| Q2 | Confirmation language before the user's language is known | English, with the built-in wording, until SAV-17/18 set the language |
| Q3 | The default model hears nothing and the fallback fails | A failed transcription: the job runs again; never recorded as no speech |

## Alternatives considered

- **One transaction around the whole job.** Simpler, but it would hold the user's lock through minutes of model calls, so "delete my data" and new shares would wait for every indexing job.
- **Skipping speech-to-text when the caption names the entity** (the ticket's first scope). Replaced by the owner's rule in record 0008.
- **Counting words for the near-empty rule.** Chinese, Japanese and Thai don't separate words with spaces, so a word count would call real speech empty.
- **Treating "no words, then a fallback error" as no speech.** That is what the model client returns, so that music-only reels don't fail when the fallback is down. The owner ruled that a provider error is never evidence of silence.
- **A translation call for each new value** (option B for P2). Keeps the post's own wording as the canonical value's source, but adds a model call and a sixth prompt per new value.
- **Asking the decision model about speech and logging it next to the audio track.** It costs tokens and invites someone to route on it again.
- **Skipping the confirmation when the language is unknown.** The default proposed for Q2; the owner chose English instead.
- **A unique key on `(saved_item_id, step)` in `enrichment_run`.** One job runs per item at a time (its job id is the item's), so the code keeps one row per step without a new migration.

## Consequences

- **New settings** in `PipelineSettings` (`app/config.py`) and `.env.example`: the media folder, download and ffmpeg limits, the near-empty rule, the is-list threshold, extraction temperature and length, the vocabulary thresholds and the worker's concurrency, tries, retry delay and timeout.
- **The image installs ffmpeg** (`apt-get install --no-install-recommends ffmpeg`), and makes `/srv/savely/media` writable by its non-root user. The API and the worker share the image.
- **Compose has a `worker` service** running `python -m app.pipeline.worker`. It reads `.env` whole, because it needs nearly every setting (database, Redis, models, pipeline, Send API), and overrides the database and Redis URLs, the ffmpeg paths and the media folder (the `media_data` volume) for the Compose network. With the dummy `OPENROUTER_API_KEY` of `.env.example`, its model calls are refused and the items it picks up end as `failed`.
- **New code:** `app/pipeline/` (steps, prompts, vocabulary, worker). `costs.py`, `media.py`, `open_values.py`, `payloads.py`, `renormalize.py`, `runs.py`, `storage.py`, `vocabulary.py`, `confirm.py` and `indexer.py` are new in the §6 layout next to the files it already lists.
- **Not done here:** resolving entities and the "which one?" question (SAV-15); the import endpoint that queues many jobs (Phase 2); a hosted media store; wiring `LocalMediaStore` into a "delete my data" command next to ingestion's eraser, which no ticket has built yet.
- **Evals:** there is no labelled set yet (`evals/labeled_saves.jsonl`, SAV-19/20). The prompts, the near-empty rule and the thresholds are first versions to score there.
- **A job that times out** (`INDEXING_JOB_TIMEOUT_SECONDS`) is not tried again by arq, and its item stays `enriching`. Search shows such items at fallback level L2.
