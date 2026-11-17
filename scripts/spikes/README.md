# Spikes

Small, reproducible experiments that back a decision record. They are not part of the deployed app.

## `model_choices`: speech-to-text and embedding comparison (SAV-7)

This spike repeats round 2 of SAV-7: the same audio clips go to four speech-to-text models, and the same sentences go to two embedding models, all through OpenRouter. The results and the models Muhammed chose are in [`docs/decisions/0008-model-choices.md`](../../docs/decisions/0008-model-choices.md).

### Safe by default

```bash
python -m scripts.spikes.model_choices --media-dir /path/outside/the/repo/fleurs
```

Run it from the repository root, in the project's virtualenv. Without `--live`, **nothing is sent and no key is needed**. The command checks every clip against the recorded hashes, then logs the plan: how many requests a live run would send and the most they could cost.

### A live run costs money and needs Muhammed's approval

SAV-22 §8: no live API calls and no spending without the owner's approval on the issue. With that approval:

```bash
export OPENROUTER_API_KEY=...   # in your shell only; never in a file in this repo

python -m scripts.spikes.model_choices \
  --media-dir /path/outside/the/repo/fleurs \
  --output-dir /path/outside/the/repo/results \
  --live \
  --spent-so-far <total charged so far>
```

| Option | Meaning |
|---|---|
| `--media-dir` | Folder with the FLEURS `manifest.json` and the WAV clips (see below) |
| `--unreferenced-dir` | Optional folder of extra PCM WAV clips without reference transcripts, such as audio taken from private reels |
| `--output-dir` | Where `report.json` is written. Needed with `--live`. Use a new folder: a folder that already holds a `report.json` is refused, so an earlier report is never overwritten |
| `--live` | Send real, paid requests |
| `--spent-so-far` | US dollars already charged for SAV-7 in earlier runs. Needed with `--live`. It was `0.08541` after rounds 1 and 2; later rounds add to it, so use the latest total in record 0008 |
| `--spend-cap` | Cumulative limit. Defaults to the owner's $2.00; it can be lowered but not raised |

What keeps a live run inside the rules:

- **The spending cap is cumulative.** Earlier spending plus this run's whole plan must fit under the cap before the first request. Each request is checked again before it is sent, and its estimate is set aside at that moment. Only a usable charge in the response replaces the estimate, so a request that fails, is cancelled, or reports no charge still counts at its estimate. Estimates are twice the catalog price. If a response charges more than expected and the total reaches the cap, the run stops before the next request and still writes what it measured.
- **The key** is read from the `OPENROUTER_API_KEY` environment variable only. The spike never reads `.env`, and never logs or writes the key.
- **Amounts must be real numbers.** `nan`, `inf` and negative values for `--spent-so-far` and `--spend-cap` are refused before anything is sent. A response whose charge is missing, not finite or negative is also counted at its estimate.
- **The report is secured before the first request.** The output folder is created and `report.json` is claimed up front; if that fails, nothing is sent. If the run breaks or is cancelled part-way, the report is still written with what was spent (`run_finished` is then `false`), and the spending is logged as well. `unconfirmed_this_run_usd` then includes the estimate of the request that was cut off: it may have been charged, so add it to `--spent-so-far` for the next run.
- **Media and results stay outside the repository.** A `--media-dir`, `--unreferenced-dir` or `--output-dir` inside the repository is refused.
- **Private clips stay private.** Clips from `--unreferenced-dir` are labelled by position (`unreferenced-1`), and their transcripts are never written. Only success, latency, cost and metadata are kept for them.
- **Failures stay visible.** No request is retried. An error response or an empty transcript is a failed request and scores as the whole reference deleted.

### What it measures

**Speech-to-text.** Every clip goes to `x-ai/grok-stt-2.0`, `microsoft/mai-transcribe-2`, `openai/gpt-transcribe` and `meta/muse-voice-transcribe-1.0` (`POST /api/v1/audio/transcriptions`). `report.json` holds:

- a model × language table: word error rate (character error rate for Japanese), successes, median and slowest latency, returned charge, and charged dollars per audio minute;
- per model: failed requests, empty transcripts, and how many responses carried a detected language, word timestamps and segment timestamps;
- every request.

**Scoring** is the same for every model (`scoring.py`): Unicode NFKC; case folding, with Turkish dotted and dotless I handled the Turkish way; punctuation, symbols and separators become spaces; spaces are collapsed. Accents, combining marks and digits are kept. Numbers are not spelled out and nothing is transliterated. The rate is all edit errors over all reference words (or characters) of a language's clips.

**Embeddings.** The reference sentences are embedded by `openai/text-embedding-3-large` and `qwen/qwen3-embedding-8b`, once with `dimensions=1024` and once at full size (`POST /api/v1/embeddings`). The returned vectors must have the expected size and be finite and non-zero. Then, for each sentence, the spike checks whether its translation ranks first in each other language, and whether its translations fill the top places among all sentences in the other languages.

**Not repeated by this spike:** the round 1 access checks (Gemini Decision 1, DeepSeek V4.1 Flash, the Gemini 2.5 Flash escalation), the automated judging of private-reel transcripts, and round 3 (the escalation and text-model comparison). The Reviewer ran those by hand; record 0008 has the results.

### The test clips

The clips are from Google's **FLEURS** test set: the same three sentences (ids 1914, 1771 and 1669) in English, Turkish, Arabic, Spanish, Hindi and Japanese.

> FLEURS: Few-shot Learning Evaluation of Universal Representations of Speech. Conneau et al. (2022), Google. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source: <https://huggingface.co/datasets/google/fleurs>, revision `70bb2e84b976b7e960aa89f1c648e09c59f894dd`, test split.

The audio and the sentences are not in this repository. [`model_choices/fleurs_provenance.json`](model_choices/fleurs_provenance.json) records, for each of the 18 clips, the language, the sentence id, the dataset's file name, the clip length, and the SHA-256 of the converted audio and of the reference sentence. The spike refuses to run if a clip or a sentence doesn't match.

To rebuild the media folder:

1. For each language (`en_us`, `tr_tr`, `ar_eg`, `es_419`, `hi_in`, `ja_jp`), take `data/<language>/test.tsv` at the revision above. Find the rows with ids 1914, 1771 and 1669 whose file name matches `original_file` in the provenance file. The third column is the reference sentence.
2. Take those audio files from the same revision's test audio. Convert each to mono 16 kHz PCM16 WAV without trimming, and name it `<language>-<id>.wav`. The recorded clips were converted with macOS `afconvert`; the hash check tells you whether your conversion matches.
3. Write `manifest.json` next to the clips:

   ```json
   {
     "dataset": "google/fleurs",
     "revision": "70bb2e84b976b7e960aa89f1c648e09c59f894dd",
     "split": "test",
     "samples": [
       {"language": "en_us", "id": 1914, "reference_raw": "<the sentence>", "audio_file": "en_us-1914.wav"}
     ]
   }
   ```

Extra keys in the manifest are ignored. Only the file name of `audio_file` is used, and it is looked up in `--media-dir`.

### Tests

`tests/spikes/` covers the scoring, the spending cap, the clip checks, the request shapes and the command line. The tests use synthetic clips and a mocked HTTP transport, so `pytest` stays offline.
