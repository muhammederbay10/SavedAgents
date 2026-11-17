"""Speech-to-text comparison: the same clips through every model, failures kept visible.

FLEURS clips have a reference sentence and get an error rate. Other clips (for
example private reels) have no reference: only success, latency, cost and
metadata are recorded for them, and their transcripts are never kept.
"""

import logging
import statistics
import wave
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Literal

from scripts.spikes.model_choices.fleurs import ClipSetError, FleursSample
from scripts.spikes.model_choices.openrouter import ApiResult, OpenRouterClient
from scripts.spikes.model_choices.scoring import (
    ErrorCount,
    Metric,
    combine,
    metric_for,
    score_transcript,
)
from scripts.spikes.model_choices.settings import (
    ESTIMATE_SAFETY_FACTOR,
    SECONDS_PER_MINUTE,
    SpeechToTextModel,
)

logger = logging.getLogger(__name__)

ClipGroup = Literal["fleurs", "unreferenced"]


@dataclass(frozen=True)
class Clip:
    """One audio clip to transcribe.

    Attributes:
        group: `fleurs` for clips with a reference, `unreferenced` for the rest.
        clip_id: A label that does not reveal private file names.
        language: FLEURS language code; `None` for unreferenced clips.
        reference: The reference sentence; `None` for unreferenced clips.
        audio: The clip's bytes (PCM WAV).
        duration_seconds: Clip length.
    """

    group: ClipGroup
    clip_id: str
    language: str | None
    reference: str | None
    audio: bytes = field(repr=False)
    duration_seconds: float


@dataclass(frozen=True)
class TranscriptionOutcome:
    """What one model returned for one clip.

    Attributes:
        model_id: The exact OpenRouter model id.
        group: The clip's group.
        clip_id: The clip's label.
        language: The clip's FLEURS language code, if it has one.
        status: HTTP status, or `None` when no response arrived.
        error: The exception's class name when no response arrived.
        latency_seconds: Request time.
        returned_cost_usd: The charge the API reported, if any.
        duration_seconds: Clip length.
        succeeded: HTTP 200 with a non-empty transcript.
        detected_language: The language the model reported, if any.
        has_word_timestamps: Whether word timestamps came back.
        has_segment_timestamps: Whether segment timestamps came back.
        edit_errors: Edit errors against the reference; `None` without a reference.
        reference_units: Words or characters in the reference; `None` without one.
        transcript: The transcript, kept only for FLEURS clips.
    """

    model_id: str
    group: ClipGroup
    clip_id: str
    language: str | None
    status: int | None
    error: str | None
    latency_seconds: float
    returned_cost_usd: float | None
    duration_seconds: float
    succeeded: bool
    detected_language: str | None
    has_word_timestamps: bool
    has_segment_timestamps: bool
    edit_errors: int | None
    reference_units: int | None
    transcript: str | None


@dataclass(frozen=True)
class LanguageSummary:
    """One cell of the model x language table (FLEURS clips only)."""

    language: str
    model_id: str
    metric: Metric
    error_percent: float
    edit_errors: int
    reference_units: int
    succeeded: int
    clips: int
    median_latency_seconds: float
    slowest_latency_seconds: float
    returned_cost_usd: float
    charged_usd_per_minute: float | None


@dataclass(frozen=True)
class ModelSummary:
    """One model over every clip, including the unreferenced ones."""

    model_id: str
    clips: int
    succeeded: int
    request_failures: int
    empty_transcripts: int
    median_latency_seconds: float
    slowest_latency_seconds: float
    returned_cost_usd: float
    charged_usd_per_minute: float | None
    language_metadata_responses: int
    word_timestamp_responses: int
    segment_timestamp_responses: int
    unreferenced_clips: int
    unreferenced_succeeded: int


def fleurs_clips(samples: Iterable[FleursSample]) -> list[Clip]:
    """Turn verified FLEURS samples into clips with references."""
    return [
        Clip(
            group="fleurs",
            clip_id=f"{sample.language}-{sample.sentence_id}",
            language=sample.language,
            reference=sample.reference,
            audio=sample.audio,
            duration_seconds=sample.duration_seconds,
        )
        for sample in samples
    ]


def unreferenced_clips(directory: Path) -> list[Clip]:
    """Load every PCM WAV file in a folder as a clip without a reference.

    Clips are labelled by position, so private file names never reach the report.

    Args:
        directory: A folder outside the repository.

    Returns:
        The clips, ordered by file name.

    Raises:
        ClipSetError: If the folder has no WAV file, or one is empty or cannot be
            read as PCM WAV.
    """
    paths = sorted(directory.glob("*.wav"))
    if not paths:
        raise ClipSetError("the folder for unreferenced clips has no .wav file")
    return [_unreferenced_clip(path, position) for position, path in enumerate(paths, start=1)]


def _unreferenced_clip(path: Path, position: int) -> Clip:
    try:
        audio = path.read_bytes()
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            frame_rate = wav_file.getframerate()
    except (OSError, EOFError, wave.Error) as error:
        raise _unreadable_clip(position) from error
    if frames <= 0 or frame_rate <= 0:
        raise _unreadable_clip(position)
    duration_seconds = frames / frame_rate
    return Clip(
        group="unreferenced",
        clip_id=f"unreferenced-{position}",
        language=None,
        reference=None,
        audio=audio,
        duration_seconds=duration_seconds,
    )


def _unreadable_clip(position: int) -> ClipSetError:
    # The message names the position, not the file: file names can be private.
    return ClipSetError(f"unreferenced clip {position} is not a readable, non-empty PCM WAV file")


def estimate_transcription_usd(model: SpeechToTextModel, duration_seconds: float) -> float:
    """Return the most one transcription is expected to cost."""
    minutes = duration_seconds / SECONDS_PER_MINUTE
    return minutes * model.catalog_usd_per_minute * ESTIMATE_SAFETY_FACTOR


async def run_comparison(
    client: OpenRouterClient,
    models: Sequence[SpeechToTextModel],
    clips: Sequence[Clip],
    outcomes: list[TranscriptionOutcome],
) -> None:
    """Send every clip to every model, one request at a time, without retries.

    A failed request is recorded, not repeated, so failures stay in the results.

    Args:
        client: The OpenRouter client.
        models: The models to compare.
        clips: The clips; every model gets the same ones.
        outcomes: Filled as requests finish, so a stopped run keeps what it measured.

    Raises:
        SpendCapExceededError: If the next request could pass the spending cap.
    """
    for clip in clips:
        for model in models:
            estimate_usd = estimate_transcription_usd(model, clip.duration_seconds)
            result = await client.transcribe(model, clip.audio, estimate_usd)
            outcome = build_outcome(model.model_id, clip, result)
            outcomes.append(outcome)
            logger.info(
                "transcription_finished",
                extra={
                    "model": outcome.model_id,
                    "clip": outcome.clip_id,
                    "status": outcome.status,
                    "succeeded": outcome.succeeded,
                    "latency_seconds": round(outcome.latency_seconds, 3),
                },
            )


def build_outcome(model_id: str, clip: Clip, result: ApiResult) -> TranscriptionOutcome:
    """Turn an API result into an outcome, scoring it when the clip has a reference.

    A failed request or an empty transcript scores as the whole reference deleted.

    Args:
        model_id: The model that was called.
        clip: The clip that was sent.
        result: What the API returned.

    Returns:
        The outcome. The transcript is kept only for FLEURS clips.
    """
    text = result.body.get("text")
    transcript = text.strip() if result.is_success and isinstance(text, str) else ""
    detected_language = result.body.get("language")
    score = (
        score_transcript(clip.reference, transcript, clip.language)
        if clip.reference is not None and clip.language is not None
        else None
    )
    return TranscriptionOutcome(
        model_id=model_id,
        group=clip.group,
        clip_id=clip.clip_id,
        language=clip.language,
        status=result.status,
        error=result.error,
        latency_seconds=result.latency_seconds,
        returned_cost_usd=result.returned_cost_usd,
        duration_seconds=clip.duration_seconds,
        succeeded=bool(transcript),
        detected_language=detected_language if isinstance(detected_language, str) else None,
        has_word_timestamps=_is_non_empty_list(result.body.get("words")),
        has_segment_timestamps=_is_non_empty_list(result.body.get("segments")),
        edit_errors=score.edit_errors if score else None,
        reference_units=score.reference_units if score else None,
        transcript=transcript if clip.group == "fleurs" else None,
    )


def _is_non_empty_list(value: object) -> bool:
    return isinstance(value, list) and bool(value)


def summarize_by_language(outcomes: Sequence[TranscriptionOutcome]) -> list[LanguageSummary]:
    """Build the model x language table from the FLEURS outcomes.

    The error rate is the micro average: all edit errors over all reference units.
    """
    cells: dict[tuple[str, str], list[TranscriptionOutcome]] = {}
    for outcome in outcomes:
        if outcome.language is not None and outcome.reference_units is not None:
            cells.setdefault((outcome.language, outcome.model_id), []).append(outcome)
    return [
        _language_summary(language, model_id, cell) for (language, model_id), cell in cells.items()
    ]


def _language_summary(
    language: str, model_id: str, cell: Sequence[TranscriptionOutcome]
) -> LanguageSummary:
    errors = combine(
        ErrorCount(outcome.edit_errors or 0, outcome.reference_units or 0) for outcome in cell
    )
    latencies = [outcome.latency_seconds for outcome in cell]
    return LanguageSummary(
        language=language,
        model_id=model_id,
        metric=metric_for(language),
        error_percent=errors.error_percent,
        edit_errors=errors.edit_errors,
        reference_units=errors.reference_units,
        succeeded=sum(outcome.succeeded for outcome in cell),
        clips=len(cell),
        median_latency_seconds=statistics.median(latencies),
        slowest_latency_seconds=max(latencies),
        returned_cost_usd=_returned_cost_usd(cell),
        charged_usd_per_minute=_charged_usd_per_minute(cell),
    )


def summarize_by_model(outcomes: Sequence[TranscriptionOutcome]) -> list[ModelSummary]:
    """Summarize each model over all of its clips, with failures counted separately."""
    by_model: dict[str, list[TranscriptionOutcome]] = {}
    for outcome in outcomes:
        by_model.setdefault(outcome.model_id, []).append(outcome)
    return [_model_summary(model_id, group) for model_id, group in by_model.items()]


def _model_summary(model_id: str, outcomes: Sequence[TranscriptionOutcome]) -> ModelSummary:
    answered = [outcome for outcome in outcomes if outcome.status == HTTPStatus.OK]
    latencies = [outcome.latency_seconds for outcome in outcomes]
    unreferenced = [outcome for outcome in outcomes if outcome.group == "unreferenced"]
    return ModelSummary(
        model_id=model_id,
        clips=len(outcomes),
        succeeded=sum(outcome.succeeded for outcome in outcomes),
        request_failures=len(outcomes) - len(answered),
        empty_transcripts=sum(not outcome.succeeded for outcome in answered),
        median_latency_seconds=statistics.median(latencies),
        slowest_latency_seconds=max(latencies),
        returned_cost_usd=_returned_cost_usd(outcomes),
        charged_usd_per_minute=_charged_usd_per_minute(outcomes),
        language_metadata_responses=sum(o.detected_language is not None for o in outcomes),
        word_timestamp_responses=sum(o.has_word_timestamps for o in outcomes),
        segment_timestamp_responses=sum(o.has_segment_timestamps for o in outcomes),
        unreferenced_clips=len(unreferenced),
        unreferenced_succeeded=sum(outcome.succeeded for outcome in unreferenced),
    )


def _returned_cost_usd(outcomes: Iterable[TranscriptionOutcome]) -> float:
    return sum(outcome.returned_cost_usd or 0.0 for outcome in outcomes)


def _charged_usd_per_minute(outcomes: Iterable[TranscriptionOutcome]) -> float | None:
    charged = [outcome for outcome in outcomes if outcome.returned_cost_usd is not None]
    charged_seconds = sum(outcome.duration_seconds for outcome in charged)
    if charged_seconds == 0:
        return None
    return _returned_cost_usd(charged) / (charged_seconds / SECONDS_PER_MINUTE)
