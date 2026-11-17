from pathlib import Path

import pytest

from scripts.spikes.model_choices.fleurs import ClipSetError
from scripts.spikes.model_choices.openrouter import ApiResult
from scripts.spikes.model_choices.settings import SpeechToTextModel
from scripts.spikes.model_choices.stt import (
    Clip,
    TranscriptionOutcome,
    build_outcome,
    estimate_transcription_usd,
    summarize_by_language,
    summarize_by_model,
    unreferenced_clips,
)
from tests.spikes.clip_set import wav_bytes

MODEL_ID = "vendor/model"
REFERENCE = "one two three four"


def fleurs_clip(language: str = "en_us", reference: str = REFERENCE) -> Clip:
    return Clip("fleurs", f"{language}-1", language, reference, b"wav", duration_seconds=30.0)


def private_clip() -> Clip:
    return Clip("unreferenced", "unreferenced-1", None, None, b"wav", duration_seconds=30.0)


def answer(
    text: str, latency: float = 1.0, cost: float | None = 0.001, **extra: object
) -> ApiResult:
    return ApiResult(200, latency, {"text": text, **extra}, cost)


def outcome_for(clip: Clip, result: ApiResult, model_id: str = MODEL_ID) -> TranscriptionOutcome:
    return build_outcome(model_id, clip, result)


def test_correct_transcript_succeeds_with_no_errors() -> None:
    outcome = outcome_for(fleurs_clip(), answer("One, two, three, four."))
    assert outcome.succeeded
    assert (outcome.edit_errors, outcome.reference_units) == (0, 4)


def test_empty_transcript_is_a_failure_scored_as_the_whole_reference_deleted() -> None:
    outcome = outcome_for(fleurs_clip(), answer("  "))
    assert outcome.status == 200
    assert not outcome.succeeded
    assert (outcome.edit_errors, outcome.reference_units) == (4, 4)


def test_error_response_is_a_failure_scored_as_the_whole_reference_deleted() -> None:
    rate_limited = ApiResult(429, 0.2, {"error": {"message": "slow down"}}, None)
    outcome = outcome_for(fleurs_clip(), rate_limited)
    assert not outcome.succeeded
    assert (outcome.edit_errors, outcome.reference_units) == (4, 4)


def test_language_and_timestamps_are_detected_in_a_verbose_response() -> None:
    verbose = answer(
        "one two three four",
        language="en",
        words=[{"word": "one", "start": 0.0, "end": 0.2}],
        segments=[{"id": 0, "start": 0.0, "end": 1.0, "text": "one two three four"}],
    )
    outcome = outcome_for(fleurs_clip(), verbose)
    assert outcome.detected_language == "en"
    assert outcome.has_word_timestamps
    assert outcome.has_segment_timestamps


def test_plain_response_has_no_language_or_timestamps() -> None:
    outcome = outcome_for(fleurs_clip(), answer("one two three four"))
    assert outcome.detected_language is None
    assert not outcome.has_word_timestamps
    assert not outcome.has_segment_timestamps


def test_transcript_of_an_unreferenced_clip_is_not_kept_or_scored() -> None:
    outcome = outcome_for(private_clip(), answer("words from a private reel"))
    assert outcome.succeeded
    assert outcome.transcript is None
    assert outcome.edit_errors is None


def test_language_table_uses_a_micro_average_and_keeps_failures() -> None:
    outcomes = [
        outcome_for(fleurs_clip(), answer("one two three four", latency=1.0)),
        outcome_for(fleurs_clip(reference="five six"), answer("five sex", latency=3.0)),
        outcome_for(fleurs_clip(reference="seven eight nine ten"), answer("", latency=2.0)),
    ]

    (cell,) = summarize_by_language(outcomes)

    assert (cell.language, cell.model_id, cell.metric) == ("en_us", MODEL_ID, "WER")
    assert (cell.edit_errors, cell.reference_units) == (5, 10)
    assert cell.error_percent == pytest.approx(50.0)
    assert (cell.succeeded, cell.clips) == (2, 3)
    assert cell.median_latency_seconds == pytest.approx(2.0)
    assert cell.slowest_latency_seconds == pytest.approx(3.0)


def test_cost_per_minute_is_the_returned_charge_over_the_charged_audio() -> None:
    outcomes = [
        outcome_for(fleurs_clip(), answer("one two three four", cost=0.002)),
        outcome_for(fleurs_clip(), answer("one two three four", cost=0.001)),
        outcome_for(fleurs_clip(), ApiResult(429, 0.2, {}, None)),
    ]

    (cell,) = summarize_by_language(outcomes)

    assert cell.returned_cost_usd == pytest.approx(0.003)
    assert cell.charged_usd_per_minute == pytest.approx(0.003)


def test_model_summary_separates_request_failures_from_empty_transcripts() -> None:
    outcomes = [
        outcome_for(fleurs_clip(), answer("one two three four", language="en")),
        outcome_for(fleurs_clip(), answer("")),
        outcome_for(fleurs_clip(), ApiResult(429, 0.2, {}, None)),
        outcome_for(fleurs_clip(), ApiResult(None, 5.0, {}, None, "ReadTimeout")),
        outcome_for(private_clip(), answer("words from a private reel")),
    ]

    (summary,) = summarize_by_model(outcomes)

    assert (summary.clips, summary.succeeded) == (5, 2)
    assert summary.request_failures == 2
    assert summary.empty_transcripts == 1
    assert summary.language_metadata_responses == 1
    assert (summary.unreferenced_clips, summary.unreferenced_succeeded) == (1, 1)


def test_models_are_summarized_separately() -> None:
    outcomes = [
        outcome_for(fleurs_clip(), answer("one two three four"), "vendor/first"),
        outcome_for(fleurs_clip(), answer(""), "vendor/second"),
    ]
    assert [summary.model_id for summary in summarize_by_model(outcomes)] == [
        "vendor/first",
        "vendor/second",
    ]
    assert [cell.error_percent for cell in summarize_by_language(outcomes)] == [0.0, 100.0]


def test_estimate_doubles_the_catalog_price() -> None:
    model = SpeechToTextModel(MODEL_ID, "json", catalog_usd_per_minute=0.003)
    assert estimate_transcription_usd(model, duration_seconds=30.0) == pytest.approx(0.003)


def test_unreferenced_clips_are_labelled_by_position_not_file_name(tmp_path: Path) -> None:
    (tmp_path / "restaurant-name.wav").write_bytes(wav_bytes(seconds=2.0))
    (tmp_path / "another-name.wav").write_bytes(wav_bytes(seconds=1.0))

    clips = unreferenced_clips(tmp_path)

    assert [clip.clip_id for clip in clips] == ["unreferenced-1", "unreferenced-2"]
    assert [clip.duration_seconds for clip in clips] == [pytest.approx(1.0), pytest.approx(2.0)]
    assert all(clip.reference is None and clip.language is None for clip in clips)


def test_folder_without_wav_files_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ClipSetError, match=r"no \.wav file"):
        unreferenced_clips(tmp_path)


def test_file_that_is_not_pcm_wav_is_rejected_without_naming_it(tmp_path: Path) -> None:
    (tmp_path / "restaurant-name.wav").write_bytes(b"not a wav file")
    with pytest.raises(ClipSetError) as error:
        unreferenced_clips(tmp_path)
    assert "restaurant-name" not in str(error.value)


def test_empty_wav_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "empty.wav").write_bytes(wav_bytes(seconds=0.0))
    with pytest.raises(ClipSetError, match="unreferenced clip 1"):
        unreferenced_clips(tmp_path)
