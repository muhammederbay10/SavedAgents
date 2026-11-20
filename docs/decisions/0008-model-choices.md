# 0008: Model choices, all through OpenRouter

- Status: accepted by Muhammed in chat on 2026-11-17 (recorded on SAV-7 and in SAV-22's owner-approved updates). These are first choices from a small test; the evals (SAV-19, SAV-20) re-check them.
- Date: 2026-11-17
- Ticket: SAV-7 (the ticket first asked for `0003-…`, then `0005-…`; both numbers were taken, so this is `0008`)

## Context

`docs/ARCHITECTURE.md` §3.9 names a model for each role, but none of them had been tried on our own saves. Gemini Decision 1 launched on 11 Nov 2026, the speech-to-text and embedding models were still open (SAV-22 §11), and Muhammed decided that every model call goes through **OpenRouter** with one key. SavedAgent is a product for **any language**, so a model that only handles Turkish and English is not enough.

The Reviewer ran three rounds of live checks with Muhammed's approval, under a cap of $2 for the whole ticket. Muhammed chose the models after rounds 2 and 3. This record keeps the two apart: [what was measured](#what-was-measured), and [what Muhammed decided](#decision).

## Decision

Every model call goes through OpenRouter (`https://openrouter.ai/api/v1`, key `OPENROUTER_API_KEY`). The provider interfaces in `app/models/` stay, so a model can still be swapped in config.

| Role | Model (exact OpenRouter id) | Endpoint |
|---|---|---|
| Fixed decisions | `google/gemini-decision-1` | `POST /api/alpha/decisions`: its own contract, with the media in `state`, typed questions, and probabilities in the answer. Not chat completions |
| Extraction, response agent, memory extraction | `deepseek/deepseek-v4.1-flash`, pinned to a provider hosted outside China | `POST /api/v1/chat/completions` |
| Escalation for silent reels | `google/gemini-2.5-flash` | `POST /api/v1/chat/completions`, with the MP4 as input |
| Speech-to-text | `microsoft/mai-transcribe-2`; fallback `openai/gpt-transcribe` | `POST /api/v1/audio/transcriptions` |
| Embeddings | `openai/text-embedding-3-large` with `dimensions=1024` | `POST /api/v1/embeddings` |

1. **Speech-to-text fallback.** `openai/gpt-transcribe` is used when `microsoft/mai-transcribe-2` returns an error or an empty transcript. There is **no routing by language** yet: three clips per language are not enough to justify it.
2. **Transcript metadata.** MAI's detected language, word timestamps and segment timestamps are kept. The GPT fallback may return none of them; they are then stored explicitly as unavailable, not left out silently.
3. **Embedding size.** Vectors have 1024 dimensions, the size SAV-9 stores (`EMBEDDING_DIMENSION=1024`). The client checks the size of every returned vector.
4. **Speech routing uses the audio track and the transcript, never a model's opinion.**
   - No audio track: `has_speech=false`, and speech-to-text is skipped.
   - An audio track: speech-to-text always runs (MAI, then the GPT fallback on an error or an empty transcript).
   - If the final transcript is empty or nearly empty, the item has no speech.
   - No model's "has speech" answer decides the route. For videos with no audio track, Gemini Decision 1 gave speech probabilities of up to 0.86, and Gemini 2.5 Flash and Qwen 3.8 Flash claimed speech every time (see [round 1](#round-1-access-and-first-checks) and [round 3](#round-3-five-models-for-escalation-and-for-text)).
   - What counts as "nearly empty" is a documented setting of the pipeline (SAV-14). This record does not fix a number.
5. **DeepSeek's host.** `deepseek/deepseek-v4.1-flash` is pinned on OpenRouter to a provider hosted outside China. The provider is a setting, not hard-coded. Which provider that is, and where it is hosted, is checked in SAV-10; none is proven yet.
6. **Any language.** Speech-to-text, extraction, replies and evals must work in any language. Turkish examples are only examples.

**Where the owner's choice differs from the measurements' proposal:** the Reviewer proposed `qwen/qwen3-embedding-8b` at 1024 dimensions, because both embedding models scored the same and Qwen was cheaper. Muhammed chose `openai/text-embedding-3-large` at 1024 dimensions ("Yes, with OpenAI"). The other rows follow the Reviewer's proposals.

### How the escalation model was chosen

1. After round 2, Muhammed approved `google/gemini-2.5-flash`, the model tested in round 1.
2. The same day he reopened it. His reason: it is an older model, and it was only tested because the round 1 instructions said "Gemini Flash-class". He asked for [round 3](#round-3-five-models-for-escalation-and-for-text).
3. After round 3 the Reviewer proposed keeping `google/gemini-2.5-flash`. Muhammed first chose the newer `google/gemini-3.8-flash`, which was not in round 3, on condition that the Reviewer verified it.
4. He then dropped it for its price and kept **`google/gemini-2.5-flash`**: "lets keep 2.5 flash since google/gemini-3.8-flash is expensive". OpenRouter's model list showed $0.825 per million input tokens against $0.30, about 2.75 times as much, and $4.125 against $2.50 for output.

**The Gemini 3.8 Flash verification was cancelled part-way. It establishes neither a pass nor a fail, and this record draws no conclusion about that model.** It did send requests before it was stopped; they are listed under [the cancelled verification](#the-cancelled-gemini-38-flash-verification).

## What was measured

The Reviewer made all live calls. Charges are the `usage.cost` values OpenRouter returned, not estimates.

| | Returned charges |
|---|---|
| Round 1 (16 Nov) | $0.02412 |
| Round 2 (17 Nov) | $0.06130 |
| Round 3 (17 Nov) | $0.08591 |
| Cancelled Gemini 3.8 Flash verification (17 Nov) | $0.00216 |
| **Total returned charges** | **$0.17348** of the $2.00 cap |

The exact total is $0.17348408. The key's own usage counter agreed with it: $0.173484057 at 16:48 UTC on 17 Nov. One request of the cancelled verification may add to this; see [the cancelled verification](#the-cancelled-gemini-38-flash-verification).

### Round 1: access and first checks

Test media: 8 of Muhammed's real saves (3 reel videos, 3 post images, 2 reels with only a caption). They are private and stay outside the repository.

| Role | Model id | Catalog price | Latency | Charged | Result |
|---|---|---|---|---|---|
| Decisions | `google/gemini-decision-1` | $0.10/M input, output free | 2.97–8.37 s per video | $0.000202–0.000350 per video | Took all 3 videos directly, and the images and captions. Type and is-list probabilities were complete and summed to one. **Speech detection failed**, see below |
| Extraction and replies | `deepseek/deepseek-v4.1-flash` | $0.14/M input, $0.42/M output; routed charges varied | 0.63–1.85 s per extraction | $0.000047–0.000171 per extraction | JSON-schema extraction passed on 4 captions and 1 transcript. Tool selection and empty-result replies worked in Turkish, English and Spanish test cases |
| Escalation | `google/gemini-2.5-flash` | $0.30/M input, $2.50/M output, audio $1/M | 4.20–10.26 s per muted video | $0.002035–0.003684 per muted video | Named visible entities in 3 of 3 muted clips; whether the names are right is not labelled. Also claimed speech in the muted clips |
| Speech-to-text | `meta/muse-voice-transcribe-1.0` | $0.003/min | 7.72 s for the one success | $0.000848 for 17 s | Transcribed 1 of 3 reels; the others returned HTTP 429. No detected language and no timestamps |
| Embeddings | `qwen/qwen3-embedding-8b` | $0.01/M input | 0.71–1.75 s per batch | under $0.000003 per batch | 4096-dimension vectors; `dimensions=1024` worked |

- The decision model answered as version `google/gemini-decision-1-20261111`.
- **The speech-gate failure.** Three control videos had their audio removed (confirmed: no audio track, one video track). Decision 1 still returned speech probabilities of **0.784, 0.453 and 0.864**, and Gemini 2.5 Flash reported speech in all three. Neither model can be trusted alone to say whether a video has speech. This is why decision 4 checks for an audio track first.
- **Languages covered:** the speech in the three reels is Turkish; the four captions are three English and one Turkish. The English and Spanish reply checks used invented test cases.
- DeepSeek's default routing went through more than one provider, so the model id alone doesn't fix the host or the price.

### Round 2: four speech-to-text models, two embedding models

**Test clips.** 18 clips from Google's FLEURS test set: the same three sentences (ids 1914, 1771, 1669) in English, Turkish, Arabic, Spanish, Hindi and Japanese, so every model got identical audio and every language the same content. The Reviewer also ran the three real Turkish reels. No reels in other languages were available.

> FLEURS: Conneau et al. (2022), Google, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), <https://huggingface.co/datasets/google/fleurs>, revision `70bb2e84b976b7e960aa89f1c648e09c59f894dd`, test split. The original float WAV files were converted to mono 16 kHz PCM16 without trimming.

**Scoring**, the same for every model: Unicode NFKC; case folding, with Turkish dotted and dotless I handled the Turkish way; punctuation, symbols and separators become spaces; spaces are collapsed. Accents, combining marks and digits are kept; numbers are not spelled out and nothing is transliterated. The error rate is all edit errors over all reference words of a language's three clips. Japanese is scored per character. An empty transcript counts as a failure and as the whole reference deleted.

**Error rate; median / slowest latency in seconds.** WER, except Japanese (CER). Lower is better; the best in each row is bold.

| Language | Grok STT 2.0 | MAI Transcribe 2 | GPT Transcribe | Muse (baseline) |
|---|---|---|---|---|
| English | 77.97%; 1.23 / 1.23 | **3.39%**; 1.31 / 1.67 | 5.08%; 0.98 / 1.50 | 6.78%; 4.43 / 5.01 |
| Turkish | 22.73%; 1.46 / 1.65 | **6.82%**; 0.93 / 1.49 | 9.09%; 1.17 / 1.29 | 11.36%; 4.62 / 4.68 |
| Arabic | 7.27%; 1.37 / 1.74 | **3.64%**; 1.22 / 1.60 | **3.64%**; 0.98 / 1.22 | 21.82%; 3.35 / 5.25 |
| Spanish | 2.70%; 1.66 / 1.67 | **1.35%**; 0.89 / 1.52 | 4.05%; 1.09 / 1.16 | 4.05%; 5.53 / 6.92 |
| Hindi | 10.94%; 1.41 / 1.69 | **9.38%**; 0.82 / 0.90 | 14.06%; 1.04 / 1.09 | 12.50%; 4.47 / 5.09 |
| Japanese (CER) | 1.26%; 1.58 / 1.78 | 1.26%; 0.86 / 0.92 | **0.63%**; 1.33 / 1.40 | 3.14%; 5.58 / 5.92 |

Every cell had 3 of 3 non-empty transcripts, except Grok in English with 1 of 3: it returned HTTP 200 with empty text for two clips. Its 77.97% is those two empty answers counted as deletions.

**Per model, over 21 inputs (18 FLEURS clips and 3 reels):**

| Model id | Non-empty | Charged per audio minute | Language and timestamps |
|---|---|---|---|
| `x-ai/grok-stt-2.0` | 19/21 | $0.00167 | None in the plain JSON answers. One separate `verbose_json` probe returned them |
| `microsoft/mai-transcribe-2` | 21/21 | $0.00172 | Detected language, word and segment timestamps on all 21 (`verbose_json`) |
| `openai/gpt-transcribe` | 21/21 | $0.00463 | None. `verbose_json` is rejected with HTTP 400 |
| `meta/muse-voice-transcribe-1.0` | 21/21 | $0.00301 | None. No 429s in this round |

There were no HTTP errors in the 84 comparison requests.

**Real reels** (three, Turkish, with background music). There is no human transcript, so there is no error rate. A Gemini 2.5 Flash judge, which saw the original video and did not know which model wrote which transcript, rated them. This is an automated opinion, not a verified measure of accuracy.

| Model | Mean readability (of 5) | Reels with a missing or wrong name | Median / slowest latency |
|---|---|---|---|
| Grok | 4.00 | 2 of 3 | 3.23 / 3.77 s |
| MAI | 5.00 | 0 of 3 | 1.27 / 1.46 s |
| GPT | 4.33 | 2 of 3 | 2.07 / 2.28 s |
| Muse | 3.33 | 3 of 3 | 10.15 / 12.60 s |

**Embeddings.** The 18 reference sentences were embedded in one batch per configuration. For each of the 30 ordered language pairs, each of the 3 sentences had to rank its own translation first among the 3 sentences of the other language: 90 queries.

| Model id | Dimensions | Translation ranked first | Latency (one batch) | Charged (one batch) |
|---|---|---|---|---|
| `openai/text-embedding-3-large` | 1024 | 90/90 | 0.64 s | $0.00013390 |
| `openai/text-embedding-3-large` | 3072 (full) | 90/90 | 0.98 s | $0.00013390 |
| `qwen/qwen3-embedding-8b` | 1024 | 90/90 | 0.98 s | $0.00000821 |
| `qwen/qwen3-embedding-8b` | 4096 (full) | 90/90 | 1.41 s | $0.00000821 |

All four also ranked a translation first for 18 of 18 sentences when every sentence in the other five languages was a candidate (15 candidates), and put all five translations in the top five. Every vector had the requested size. Catalog prices: $0.13 per million tokens for OpenAI, $0.01 for Qwen.

### Round 3: five models for escalation and for text

Muhammed asked for this round to reconsider the escalation model, and to see whether another model is better for extraction and the response agent. The five candidates: `google/gemini-2.5-flash`, `qwen/qwen3.8-flash`, `z-ai/glm-5.3-flash`, `deepseek/deepseek-v4.1-flash-1017` and `openai/gpt-6.1-terra`. The approved `deepseek/deepseek-v4.1-flash` ran next to them as the text baseline. All six ids exist on OpenRouter.

**Escalation test.** The three reels and three muted copies of them. The round 1 muted files were gone, so the Reviewer made new ones the same way; each has no audio track and one video track, and its sampled frames match the original's. Every model got the same prompt and the same strict six-field JSON schema, with a 1,000-token output limit. Models that advertise video input got the MP4. Models that take only images got six frames per video, with no audio.

| Model id | Advertised input: video / image / audio | Catalog $ per M tokens, input / output | What it was sent | Valid JSON | Claimed speech on the 3 muted copies | Median / slowest latency | Mean charge per video |
|---|---|---|---|---|---|---|---|
| `google/gemini-2.5-flash` | yes / yes / yes | $0.30 / $2.50 | MP4 | 6/6 | 3 of 3 | 9.06 / 11.40 s | $0.00295268 |
| `qwen/qwen3.8-flash` | yes / yes / no | $0.15 / $0.47 | MP4 | 6/6 | 3 of 3 | 28.38 / 39.48 s | $0.00230086 |
| `z-ai/glm-5.3-flash` | yes / yes / no | $0.0399 / $0.0784 | MP4, rejected | none | not tested | none | none |
| `deepseek/deepseek-v4.1-flash-1017` | no / yes / no | $0.036 / $0.50 | 6 frames | 6/6 | 3 of 3 | 3.41 / 3.89 s | $0.00041889 |
| `openai/gpt-6.1-terra` | no / yes / no | $1.00 / $5.00 | 6 frames | 6/6 | 0 of 3 | 6.61 / 7.67 s | $0.00470242 |

- **GLM** rejected all six video requests with HTTP 400, and six more after one adjustment (it requires reasoning to be on). The route it was sent to answered that video and audio input is not supported. No frames were sent instead.
- **Every claim of speech on a muted copy is false.** This repeats the round 1 failure for three more models. Terra's "no speech" answers come from frames alone and say nothing about hearing.
- **Names.** All four working models returned the title shown in one reel, and it was visible in every sampled frame. For a second reel, a venue and a product name were partly confirmed. For the third, no model's full name was confirmed by both frame checks. "Confirmed" means automated checks only: two frame-reading model passes and local text recognition on six frames per reel. That text recognition was used to evaluate the answers; it is not part of the design, which has no OCR.
- Terra's later requests were served from a cache, so its mean charge is lower than a first request would cost. It does not accept a temperature setting; the others ran at temperature 0.

**Text test.** Each model got the same seven extraction inputs (four captions, and the round 2 MAI transcripts of the three reels) with a strict JSON schema, and the same 14 agent checks: choosing the search tool and writing a result reply in English, Turkish, Arabic, Spanish, Hindi and Japanese, plus a Turkish follow-up and an empty-result case. The agent checks use invented test cases.

| Model id | Valid extractions (of 7) | Returned names found word for word in the source | Tool checks (of 6) | Reply checks (of 8) | Extraction: median / slowest; mean charge | Tool and reply: median / slowest; mean charge |
|---|---|---|---|---|---|---|
| `deepseek/deepseek-v4.1-flash` (approved) | 7 | 7 of 7 | 6 | 8 | 1.61 / 12.21 s; $0.00011028 | 0.95 / 2.27 s; $0.00005574 |
| `deepseek/deepseek-v4.1-flash-1017` | 7 | 9 of 9 | 6 | 8 | 1.86 / 2.09 s; $0.00003186 | 1.45 / 2.35 s; $0.00001444 |
| `google/gemini-2.5-flash` | 7 | 7 of 8 | 6 | 8 | 1.26 / 1.66 s; $0.00031730 | 0.64 / 0.90 s; $0.00016867 |
| `qwen/qwen3.8-flash` | 7 | 7 of 7 | **4** | 8 | 2.69 / 3.87 s; $0.00008182 | 1.53 / 1.96 s; $0.00005637 |
| `z-ai/glm-5.3-flash` | **6** | 7 of 8 | 6 | 8 | 1.89 / 8.51 s; $0.00005808 | 0.88 / 1.31 s; $0.00001282 |
| `openai/gpt-6.1-terra` | 7 | 5 of 5 | 6 | 8 | 2.98 / 3.37 s; $0.00059271 | 2.13 / 2.86 s; $0.00041514 |

- "Found word for word" checks only that a returned name is inside the quote the model gave, and that the quote is in the source. It is not accuracy: nobody labelled which names should have been found.
- Qwen left the district out of the structured filter in the Turkish and Hindi tool checks. One of GLM's extractions was cut off at the output limit and was not valid JSON.
- `-1017` cost about 71% less per extraction and 74% less per agent call than the approved model, with a higher median latency.

**The Reviewer's proposals after round 3:** `google/gemini-2.5-flash` for the escalation, and keep `deepseek/deepseek-v4.1-flash` for extraction and the response agent, with `-1017` as a cheaper challenger to score on labelled saves. Muhammed's choices are [above](#how-the-escalation-model-was-chosen): Gemini 2.5 Flash for the escalation, and no change to the text model.

Round 3 made 168 calls: 156 returned HTTP 200 and 12 returned HTTP 400 (the GLM video requests). No call was rate-limited.

### The cancelled Gemini 3.8 Flash verification

This check was cancelled when Muhammed dropped the model. It had already started. Of the two runs that were cancelled, one was still queued and never started. The other sent:

| Request | Outcome | Charge |
|---|---|---|
| Model list and key usage lookups | Answered | Free |
| First model request | HTTP 400 | None returned |
| Second model request | HTTP 200 | $0.00216075 |
| A further request, interrupted by the cancellation | No response recorded. It is not known whether OpenRouter received it | None confirmed |

- **Confirmed spending:** $0.00216075. The key's usage rose by exactly that amount, from $0.171323307 before the check to $0.173484057 after it, and no other charge appeared.
- **Not confirmed:** the interrupted request. The Reviewer had reserved an estimated $0.03305610 for it. That figure is an estimate set aside before sending, not a charge and not a sign that a bill is pending. Returned charges plus this reservation come to $0.20654018, still far below the cap.
- **No result:** the plan was six videos. One charged answer is not that verification, and it says nothing about whether the model passes or fails.

## Limits of this evidence

- **Small samples.** Three clips per language; three reels, all Turkish. One wrong word moves a language's error rate by 1.4 to 2.3 points. GPT's lead in Japanese is one wrong character against two, out of 159.
- **FLEURS is read speech** with no music. Only the three reels have background music, and they have no reference transcript.
- **The reel ratings are automated** and were not checked by a person.
- **The embedding check is easy.** Three sentence meanings can't separate good models from very good ones. It shows that both models work across the six languages, not which is better.
- **Round 1 has no labels.** Probabilities are not checked for calibration, and the entities the models named are not checked for correctness. Movie and study saves were not covered.
- **Round 3 is three reels.** Frame checks were automated and looked at six frames per reel, so a name they didn't confirm may still be right. The models were sent different things (video or frames) and went through different providers, which limits comparing their latency and cost.
- **Round 3's extraction has no labels,** and its inputs are Turkish and English only. The six-language agent checks are invented cases, not real conversations.
- **Prices and routing can change.** The catalog prices are a snapshot of 16–17 Nov 2026.

So these are first choices. SAV-19 and SAV-20 score them again on labelled saves.

## How to reproduce

`scripts/spikes/model_choices/` repeats round 2: the speech-to-text comparison and the embedding retrieval check. [`scripts/spikes/README.md`](../../scripts/spikes/README.md) explains how to run it.

- By default it sends nothing. A live run needs `--live`, the key in the environment, and the amount already spent; it refuses any request that could take the total over $2. Before the first request it also checks that the amounts are real numbers and that the report can be written.
- The repository holds no audio and no sentences, only the FLEURS provenance (`fleurs_provenance.json`): dataset revision, sentence ids, file names and SHA-256 hashes. The spike refuses clips that don't match.
- **Checked offline:** fed the public FLEURS transcripts the Reviewer stored in round 2, the spike's scoring gives the same edit errors for every request and the same error rate and cost per minute for all 24 cells of the table above.
- **Not re-run live.** The Reviewer's own ad hoc scripts were not kept, so the spike's requests follow OpenRouter's documented contracts and are tested against a mocked transport. A new live run needs Muhammed's approval (SAV-22 §8). To stay on the safe side of the cap, pass the higher figure, $0.20654018, as the amount already spent.
- **The spike stays in the repository.** Muhammed decided ("Yes, keep it") that `scripts/spikes/model_choices/` and its offline tests are kept with SAV-7, so the comparison can be run again for a later model swap such as `deepseek/deepseek-v4.1-flash-1017`.
- **Not repeated by the spike:** the round 1 checks, the automated reel ratings, and round 3.

The per-request metrics of all rounds stay next to the private test media, outside the repository. They were not attached to the ticket.

## Alternatives considered

- **`meta/muse-voice-transcribe-1.0`** (Muhammed's first speech-to-text pick): 429s on two of three reels in round 1, no language or timestamps, and in round 2 the slowest model with the lowest reel ratings.
- **`x-ai/grok-stt-2.0`:** the cheapest, but it returned empty text for two of three English clips.
- **`openai/gpt-transcribe` as the default:** best in Japanese and tied in Arabic, but 2.7 times MAI's price per minute and without language or timestamps. It is the fallback.
- **`openai/whisper-large-v3`:** before Muhammed named Muse, the Reviewer's first calls transcribed 3 of 3 Turkish reels with word timestamps in 2.26–4.41 s at about $0.00045 per minute. It was not part of the round 2 comparison.
- **Self-hosted faster-whisper** (the earlier design): not tested. One provider and one key is simpler for the MVP.
- **`qwen/qwen3-embedding-8b` at 1024 dimensions:** the Reviewer's proposal; the same retrieval result at about one sixteenth of the charge. Muhammed chose OpenAI.
- **Full-size embeddings** (3072 or 4096): no better here, and three to four times the storage.
- **`google/gemini-3.8-flash` for the escalation:** newer, and chosen briefly on condition of a verification. Dropped for its price. Its verification was cancelled after one answered request, so there is no pass or fail result for it.
- **`qwen/qwen3.8-flash` for the escalation:** cheaper per video than Gemini 2.5 Flash, but a median of 28 s, and no audio input.
- **`z-ai/glm-5.3-flash`:** its advertised video input was rejected on the route tested.
- **`openai/gpt-6.1-terra` or `deepseek/deepseek-v4.1-flash-1017` for the escalation:** they take images only. Sending frames instead of the video would be a design change.
- **`deepseek/deepseek-v4.1-flash-1017` for extraction and the agent:** cheaper and passed every check, but three reels and four captions without labels don't justify a switch. It is a candidate for the evals.
- **Trusting the models for has-speech:** rejected after the muted-video tests in rounds 1 and 3.
- **A separate speech detector for files with audio:** not chosen. Running speech-to-text and reading the transcript needs no extra model.
- **Letting OpenRouter route DeepSeek to any provider:** rejected for user data. Round 1 showed it uses more than one provider by default.
- **Routing speech-to-text by language:** not justified by three clips per language.

## Consequences

- **SAV-10 (model clients):** build the clients for OpenRouter with the ids and endpoints above. The DeepSeek provider is a setting; SAV-10 picks one hosted outside China and checks that requests really go there. The decision client uses `/api/alpha/decisions`, not chat completions. The transcriber falls back to GPT on an error or an empty transcript, and marks language and timestamps as unavailable when they are missing. The embedder sends `dimensions=1024` and checks the returned size. Model ids, the base URL and `OPENROUTER_API_KEY` become settings in `app/config/models.py`, `.env.example` and the README there.
- **SAV-9 (schema):** embedding columns have 1024 dimensions.
- **SAV-14 (pipeline):** route by decision 4. Check for an audio track; with one, always run speech-to-text; set `has_speech` from the final transcript. The "nearly empty" rule is a documented setting. No model's has-speech answer is used for routing. Speech-to-text now also runs on reels that have only music, so its cost applies to every item with an audio track (MAI was charged about $0.0017 per audio minute in round 2).
- **SAV-19, SAV-20 (evals):** score speech-to-text, extraction and retrieval on labelled saves in more than one language, and track how often the fallback and the escalation run. `deepseek/deepseek-v4.1-flash-1017` is scored against the approved text model there, as a later candidate. Music-only reels show whether the "nearly empty" rule works.
- **`docs/ARCHITECTURE.md`** is updated in the same change: the models in §3.9 and §5, the speech routing in §3.3, and the comments in §6 that described the old routing. The earlier rule "skip speech-to-text when the caption already names the entity" is gone, because speech-to-text now always runs on files with an audio track.
- **Switching the embedding model later** means re-embedding every item.

## Still open

None of these is an owner decision.

1. **One request of the cancelled verification.** It is not known whether it reached OpenRouter. Nothing further is being checked: the account showed no charge for it, and its $0.03305610 reservation is kept on the books as an estimate (see [the cancelled verification](#the-cancelled-gemini-38-flash-verification)).
2. **The "nearly empty" transcript rule.** SAV-14 defines it as a setting and documents it.
3. **The DeepSeek provider.** SAV-10 finds a provider hosted outside China and verifies it.
