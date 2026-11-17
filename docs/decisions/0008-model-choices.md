# 0008: Model choices, all through OpenRouter

- Status: accepted by Muhammed in chat on 2026-11-17 (recorded on SAV-7 and in SAV-22's owner-approved updates), **except the escalation model, which is pending** a third round and Muhammed's decision. These are first choices from a small test; the evals (SAV-19, SAV-20) re-check them.
- Date: 2026-11-17
- Ticket: SAV-7 (the ticket first asked for `0003-…`, then `0005-…`; both numbers were taken, so this is `0008`)

## Context

`docs/ARCHITECTURE.md` §3.9 names a model for each role, but none of them had been tried on our own saves. Gemini Decision 1 launched on 11 Nov 2026, the speech-to-text and embedding models were still open (SAV-22 §11), and Muhammed decided that every model call goes through **OpenRouter** with one key. SavedAgent is a product for **any language**, so a model that only handles Turkish and English is not enough.

The Reviewer ran two rounds of live checks with Muhammed's approval, under a cap of $2 for the whole ticket. Muhammed then chose the models. This record keeps the two apart: [what was measured](#what-was-measured), and [what Muhammed decided](#decision). A third round is running for the escalation model; see [Pending](#pending-the-escalation-model-round-3).

## Decision

Every model call goes through OpenRouter (`https://openrouter.ai/api/v1`, key `OPENROUTER_API_KEY`). The provider interfaces in `app/models/` stay, so a model can still be swapped in config.

| Role | Model (exact OpenRouter id) | Endpoint |
|---|---|---|
| Fixed decisions | `google/gemini-decision-1` | `POST /api/alpha/decisions`: its own contract, with the media in `state`, typed questions, and probabilities in the answer. Not chat completions |
| Extraction, response agent, memory extraction | `deepseek/deepseek-v4.1-flash` | `POST /api/v1/chat/completions` |
| Escalation for silent reels | **Pending.** Not decided; see [Pending](#pending-the-escalation-model-round-3) | To be recorded with the model |
| Speech-to-text | `microsoft/mai-transcribe-2`; fallback `openai/gpt-transcribe` | `POST /api/v1/audio/transcriptions` |
| Embeddings | `openai/text-embedding-3-large` with `dimensions=1024` | `POST /api/v1/embeddings` |

1. **Speech-to-text fallback.** `openai/gpt-transcribe` is used when `microsoft/mai-transcribe-2` returns an error or an empty transcript. There is **no routing by language** yet: three clips per language are not enough to justify it.
2. **Transcript metadata.** MAI's detected language, word timestamps and segment timestamps are kept. The GPT fallback may return none of them; they are then stored explicitly as unavailable, not left out silently.
3. **Embedding size.** Vectors have 1024 dimensions, the size SAV-9 stores (`EMBEDDING_DIMENSION=1024`). The client checks the size of every returned vector.
4. **Speech gate.** The pipeline first checks whether the file has an audio track. No audio track means `has_speech=false`, and speech-to-text is skipped. For files with audio, model-based speech detection stays **provisional** (see [the speech-gate failure](#round-1-access-and-first-checks)).
5. **Any language.** Speech-to-text, extraction, replies and evals must work in any language. Turkish examples are only examples.

**Where the owner's choice differs from the measurements' proposal:** the Reviewer proposed `qwen/qwen3-embedding-8b` at 1024 dimensions, because both embedding models scored the same and Qwen was cheaper. Muhammed chose `openai/text-embedding-3-large` at 1024 dimensions ("Yes, with OpenAI"). The other decided rows follow the Reviewer's proposals.

### Pending: the escalation model (round 3)

Muhammed first approved `google/gemini-2.5-flash` for the escalation, then reopened that choice the same day. His reason: it is an older model, and it was only tested because the round 1 instructions said "Gemini Flash-class". **No escalation model is selected until he decides after round 3.**

Round 3 compares five models through OpenRouter: `google/gemini-2.5-flash`, `qwen/qwen3.8-flash`, `z-ai/glm-5.3-flash`, `deepseek/deepseek-v4.1-flash-1017` and `openai/gpt-6.1-terra`.

- **Escalation:** the round 1 test on the same three reels and their three muted copies, with one prompt and one JSON schema for every model. Models that take only images get frames sampled from the video, and are labelled as such.
- **Extraction and response agent:** the round 1 extraction and tool-calling checks for the same five models, next to the current `deepseek/deepseek-v4.1-flash`. This part is a comparison only. `deepseek/deepseek-v4.1-flash` stays the approved model unless Muhammed changes it.

Round 3's results, its charges and Muhammed's decision are added to this record when they exist. Nothing in this record is a round 3 result.

## What was measured

The Reviewer made all live calls. Charges are the `usage.cost` values OpenRouter returned, not estimates.

| | Returned charges |
|---|---|
| Round 1 (16 Nov) | $0.02412 |
| Round 2 (17 Nov) | $0.06130 |
| **Total of rounds 1 and 2** | **$0.08541** of the $2.00 cap |

The key's own usage counter agreed with the total to rounding. Round 3's charges are not in this table yet; it must stop before the ticket's total would pass $1.50.

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

## Limits of this evidence

- **Small samples.** Three clips per language; three reels, all Turkish. One wrong word moves a language's error rate by 1.4 to 2.3 points. GPT's lead in Japanese is one wrong character against two, out of 159.
- **FLEURS is read speech** with no music. Only the three reels have background music, and they have no reference transcript.
- **The reel ratings are automated** and were not checked by a person.
- **The embedding check is easy.** Three sentence meanings can't separate good models from very good ones. It shows that both models work across the six languages, not which is better.
- **Round 1 has no labels.** Probabilities are not checked for calibration, and the entities the models named are not checked for correctness. Movie and study saves were not covered.
- **Prices and routing can change.** The catalog prices are a snapshot of 16–17 Nov 2026.

So these are first choices. SAV-19 and SAV-20 score them again on labelled saves.

## How to reproduce

`scripts/spikes/model_choices/` repeats round 2: the speech-to-text comparison and the embedding retrieval check. [`scripts/spikes/README.md`](../../scripts/spikes/README.md) explains how to run it.

- By default it sends nothing. A live run needs `--live`, the key in the environment, and the amount already spent; it refuses any request that could take the total over $2.
- The repository holds no audio and no sentences, only the FLEURS provenance (`fleurs_provenance.json`): dataset revision, sentence ids, file names and SHA-256 hashes. The spike refuses clips that don't match.
- **Checked offline:** fed the public FLEURS transcripts the Reviewer stored in round 2, the spike's scoring gives the same edit errors for every request and the same error rate and cost per minute for all 24 cells of the table above.
- **Not re-run live.** The Reviewer's own ad hoc scripts were not kept, so the spike's requests follow OpenRouter's documented contracts and are tested against a mocked transport. A new live run needs Muhammed's approval (SAV-22 §8).
- **Not repeated by the spike:** the round 1 checks and the automated reel ratings.

The per-request metrics of both rounds stay next to the private test media, outside the repository. They were not attached to the ticket.

## Alternatives considered

- **`meta/muse-voice-transcribe-1.0`** (Muhammed's first speech-to-text pick): 429s on two of three reels in round 1, no language or timestamps, and in round 2 the slowest model with the lowest reel ratings.
- **`x-ai/grok-stt-2.0`:** the cheapest, but it returned empty text for two of three English clips.
- **`openai/gpt-transcribe` as the default:** best in Japanese and tied in Arabic, but 2.7 times MAI's price per minute and without language or timestamps. It is the fallback.
- **`openai/whisper-large-v3`:** before Muhammed named Muse, the Reviewer's first calls transcribed 3 of 3 Turkish reels with word timestamps in 2.26–4.41 s at about $0.00045 per minute. It was not part of the round 2 comparison.
- **Self-hosted faster-whisper** (the earlier design): not tested. One provider and one key is simpler for the MVP.
- **`qwen/qwen3-embedding-8b` at 1024 dimensions:** the Reviewer's proposal; the same retrieval result at about one sixteenth of the charge. Muhammed chose OpenAI.
- **Full-size embeddings** (3072 or 4096): no better here, and three to four times the storage.
- **Trusting the models for has-speech:** rejected after the muted-video test.
- **Routing speech-to-text by language:** not justified by three clips per language.

## Consequences

- **SAV-10 (model clients):** build the clients for OpenRouter with the ids and endpoints above. The escalation model id is not known yet, so it must be a setting with no model assumed in code. The decision client uses `/api/alpha/decisions`, not chat completions. The transcriber falls back to GPT on an error or an empty transcript, and marks language and timestamps as unavailable when they are missing. The embedder sends `dimensions=1024` and checks the returned size. Model ids, the base URL and `OPENROUTER_API_KEY` become settings in `app/config.py`, `.env.example` and the README there.
- **SAV-9 (schema):** embedding columns have 1024 dimensions.
- **SAV-14 (pipeline):** check for an audio track before speech-to-text, and skip speech-to-text when there is none. The speech decision for files with audio is provisional (open point 1).
- **SAV-19, SAV-20 (evals):** score speech-to-text, extraction and retrieval on labelled saves in more than one language, and track how often the fallback and the escalation run.
- **`docs/ARCHITECTURE.md` §3.9 and §5** are updated to these models in this ticket, after SAV-26's design change lands, because SAV-26 edits the same sections. The escalation row stays "pending" there until Muhammed decides.
- **Switching the embedding model later** means re-embedding every item.

## Open points for Muhammed

1. **Speech detection for files with audio.** An audio track doesn't mean speech (a reel can have only music). No detector is chosen yet, so SAV-14 needs a rule for files that have audio.
2. **Where DeepSeek runs.** OpenRouter routed `deepseek/deepseek-v4.1-flash` to more than one provider. Whether to pin a provider for user data (SAV-22 §11, question 3) is still open.
3. **The escalation model.** Pending round 3 (see [Pending](#pending-the-escalation-model-round-3)). Round 3 may also lead Muhammed to change the extraction and response-agent model; until he says so, it stays `deepseek/deepseek-v4.1-flash`.
