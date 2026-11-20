"""Speech routing: the audio track and the transcript decide, never a model's opinion (SAV-7)."""

import uuid
from pathlib import Path

import pytest

from app.models.clients.transcriber import FallbackTranscriber
from app.models.errors import ModelUnavailableError, TranscriptionFailedError
from app.models.fakes import FakeTranscriber
from app.pipeline.media.storage import LocalMediaStore
from app.pipeline.media.tools import AUDIO_FORMAT, MediaProbe
from app.pipeline.steps.fetch import MediaKind, StoredMedia, UnavailableReason
from app.pipeline.steps.transcribe import (
    IncompleteTranscriptionError,
    SpeechOutcome,
    SpeechToText,
    count_speech_characters,
)
from tests.pipeline.support import (
    FALLBACK_STT_MODEL,
    PRIMARY_STT_MODEL,
    FakeMediaTools,
    transcript,
)

MIN_SPEECH_CHARACTERS = 4


def unavailable_error(model: str) -> ModelUnavailableError:
    return ModelUnavailableError("synthetic outage", operation="transcribe", model=model)


async def stored_video(tmp_path: Path, *, has_audio: bool) -> tuple[LocalMediaStore, StoredMedia]:
    store = LocalMediaStore(tmp_path)
    key = await store.put(uuid.uuid4(), uuid.uuid4(), "video.mp4", b"synthetic video copy")
    media = StoredMedia(kind=MediaKind.VIDEO, media_key=key, has_audio=has_audio)
    return store, media


def speech_to_text(
    store: LocalMediaStore, primary: FakeTranscriber, fallback: FakeTranscriber
) -> SpeechToText:
    return SpeechToText(
        FallbackTranscriber(primary=primary, fallback=fallback),
        FakeMediaTools(probe_result=MediaProbe(has_video=True, has_audio=True, duration_seconds=3)),
        store,
        min_speech_characters=MIN_SPEECH_CHARACTERS,
    )


async def test_media_without_an_audio_track_skips_speech_to_text(tmp_path: Path) -> None:
    store, media = await stored_video(tmp_path, has_audio=False)
    primary, fallback = FakeTranscriber([]), FakeTranscriber([])

    result = await speech_to_text(store, primary, fallback).transcribe(media)

    assert result.outcome is SpeechOutcome.NO_AUDIO_TRACK
    assert not result.has_speech
    assert primary.calls == [] and fallback.calls == []


async def test_an_item_without_media_has_no_speech(tmp_path: Path) -> None:
    primary, fallback = FakeTranscriber([]), FakeTranscriber([])
    media = StoredMedia.unavailable(UnavailableReason.NO_MEDIA_LINK)

    result = await speech_to_text(LocalMediaStore(tmp_path), primary, fallback).transcribe(media)

    assert result.outcome is SpeechOutcome.NO_MEDIA
    assert primary.calls == []


async def test_an_audio_track_always_goes_to_speech_to_text_as_wav(tmp_path: Path) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([transcript("Kronotrop'a gittik, harika bir kahve")])

    result = await speech_to_text(store, primary, FakeTranscriber([])).transcribe(media)

    assert result.outcome is SpeechOutcome.SPEECH
    assert result.speech_text == "Kronotrop'a gittik, harika bir kahve"
    assert [audio.format for audio in primary.calls] == [AUDIO_FORMAT]


async def test_an_empty_default_transcript_falls_back_to_the_second_model(tmp_path: Path) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([transcript("")])
    fallback = FakeTranscriber(
        [transcript("We tried the new ramen place", model=FALLBACK_STT_MODEL)]
    )

    result = await speech_to_text(store, primary, fallback).transcribe(media)

    assert result.has_speech
    output = result.to_output()
    assert output["model"] == FALLBACK_STT_MODEL
    assert [attempt["outcome"] for attempt in output["attempts"]] == ["empty", "text"]  # type: ignore[index, union-attr]


async def test_a_default_model_error_falls_back_to_the_second_model(tmp_path: Path) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([unavailable_error(PRIMARY_STT_MODEL)])
    fallback = FakeTranscriber([transcript("Bu filmi mutlaka izleyin", model=FALLBACK_STT_MODEL)])

    result = await speech_to_text(store, primary, fallback).transcribe(media)

    assert result.has_speech
    assert len(fallback.calls) == 1


async def test_an_empty_final_transcript_means_no_speech(tmp_path: Path) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([transcript("")])
    fallback = FakeTranscriber([transcript("", model=FALLBACK_STT_MODEL)])

    result = await speech_to_text(store, primary, fallback).transcribe(media)

    assert result.outcome is SpeechOutcome.NO_SPEECH
    assert result.speech_text is None


@pytest.mark.parametrize("text", ["♪ ♪ ♪", "ok!", "...", "嗯", "  ¡ah! "])
async def test_a_near_empty_final_transcript_means_no_speech(tmp_path: Path, text: str) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([transcript(text)])

    result = await speech_to_text(store, primary, FakeTranscriber([])).transcribe(media)

    assert result.outcome is SpeechOutcome.NO_SPEECH


@pytest.mark.parametrize(
    "text",
    ["這家餐廳很好吃", "สวัสดีครับ", "مطعم جميل", "café", "1984"],
    ids=["chinese", "thai", "arabic", "latin", "digits"],
)
async def test_short_speech_in_any_script_counts_as_speech(tmp_path: Path, text: str) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([transcript(text)])

    result = await speech_to_text(store, primary, FakeTranscriber([])).transcribe(media)

    assert result.outcome is SpeechOutcome.SPEECH


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("♪ ... !!", 0),
        ("hi there", 7),
        ("你好", 2),
        ("สวัสดี", 6),
        ("नमस्ते", 6),
        ("٣ مطاعم", 6),
    ],
)
def test_speech_characters_are_letters_digits_and_marks_in_any_script(
    text: str, expected: int
) -> None:
    assert count_speech_characters(text) == expected


async def test_a_failure_on_both_models_is_an_error_not_silence(tmp_path: Path) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([unavailable_error(PRIMARY_STT_MODEL)])
    fallback = FakeTranscriber([unavailable_error(FALLBACK_STT_MODEL)])

    with pytest.raises(TranscriptionFailedError):
        await speech_to_text(store, primary, fallback).transcribe(media)


async def test_no_words_then_a_fallback_error_is_an_error_not_silence(tmp_path: Path) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    primary = FakeTranscriber([transcript("")])
    fallback = FakeTranscriber([unavailable_error(FALLBACK_STT_MODEL)])

    with pytest.raises(IncompleteTranscriptionError):
        await speech_to_text(store, primary, fallback).transcribe(media)


async def test_missing_fallback_timestamps_are_unavailable_and_unpriced_calls_counted(
    tmp_path: Path,
) -> None:
    store, media = await stored_video(tmp_path, has_audio=True)
    fallback_answer = transcript("a fallback transcript", model=FALLBACK_STT_MODEL)
    primary = FakeTranscriber([transcript("")])
    fallback = FakeTranscriber([fallback_answer])

    output = (await speech_to_text(store, primary, fallback).transcribe(media)).to_output()

    assert output["segments"] is None
    assert output["words"] is None
    assert output["costs"] == {"cost_usd": 0.0003, "calls": 2, "unpriced_calls": 1}
