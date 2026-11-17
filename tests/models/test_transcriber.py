import logging

import pytest

from app.models.base import (
    Audio,
    Transcript,
    TranscriptionAttempt,
    TranscriptSegment,
    TranscriptWord,
)
from app.models.errors import (
    ModelResponseError,
    ModelUnavailableError,
    TranscriptionFailedError,
)
from app.models.fakes import FakeTranscriber
from app.models.openrouter import OpenRouterTransport
from app.models.transcriber import FallbackTranscriber, OpenRouterTranscriber
from tests.models.conftest import MAX_ATTEMPTS, Connect
from tests.models.support import (
    TIMEOUT_SECONDS,
    TRANSCRIPTIONS_URL,
    error_response,
    fixture_response,
    json_response,
    load_fixture,
)

DEFAULT_MODEL = "microsoft/mai-transcribe-2"
FALLBACK_MODEL = "openai/gpt-transcribe"
CLIP = Audio(data=b"audio-bytes", format="wav")
SPOKEN_TEXT = "Welcome to Example Roasters."


def default_transcriber(transport: OpenRouterTransport) -> OpenRouterTranscriber:
    return OpenRouterTranscriber(
        transport,
        url=TRANSCRIPTIONS_URL,
        model=DEFAULT_MODEL,
        response_format="verbose_json",
        timeout_seconds=TIMEOUT_SECONDS,
    )


def fallback_transcriber(transport: OpenRouterTransport) -> OpenRouterTranscriber:
    return OpenRouterTranscriber(
        transport,
        url=TRANSCRIPTIONS_URL,
        model=FALLBACK_MODEL,
        response_format="json",
        timeout_seconds=TIMEOUT_SECONDS,
    )


def with_fallback(transport: OpenRouterTransport) -> FallbackTranscriber:
    return FallbackTranscriber(
        primary=default_transcriber(transport), fallback=fallback_transcriber(transport)
    )


def empty_transcript_response() -> dict[str, object]:
    body = load_fixture("transcription_verbose.json")
    body.update(text="", segments=[], words=[])
    return body


async def test_default_model_is_asked_for_language_and_timestamps(connect: Connect) -> None:
    server, transport = connect([fixture_response("transcription_verbose.json")])

    await default_transcriber(transport).transcribe(CLIP)

    assert server.only_request.url == TRANSCRIPTIONS_URL
    assert server.only_request.body == {
        "model": DEFAULT_MODEL,
        "input_audio": {"data": "YXVkaW8tYnl0ZXM=", "format": "wav"},
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"],
    }


async def test_text_only_model_is_not_asked_for_timestamps(connect: Connect) -> None:
    server, transport = connect([fixture_response("transcription_plain.json")])

    await fallback_transcriber(transport).transcribe(CLIP)

    assert server.only_request.body == {
        "model": FALLBACK_MODEL,
        "input_audio": {"data": "YXVkaW8tYnl0ZXM=", "format": "wav"},
        "response_format": "json",
    }


async def test_default_success_keeps_language_and_word_and_segment_timestamps(
    connect: Connect,
) -> None:
    server, transport = connect([fixture_response("transcription_verbose.json")])

    transcript = await with_fallback(transport).transcribe(CLIP)

    assert len(server.requests) == 1
    assert transcript.text == SPOKEN_TEXT
    assert transcript.model == DEFAULT_MODEL
    assert transcript.language == "en"
    assert transcript.duration_seconds == 2.4
    assert transcript.segments == (
        TranscriptSegment(start_seconds=0.0, end_seconds=2.4, text=SPOKEN_TEXT),
    )
    assert transcript.words == (
        TranscriptWord(start_seconds=0.0, end_seconds=0.5, word="Welcome"),
        TranscriptWord(start_seconds=0.5, end_seconds=0.7, word="to"),
        TranscriptWord(start_seconds=0.7, end_seconds=1.4, word="Example"),
        TranscriptWord(start_seconds=1.4, end_seconds=2.4, word="Roasters."),
    )
    assert transcript.cost_usd == 0.0000667
    assert transcript.attempts == (TranscriptionAttempt(model=DEFAULT_MODEL, outcome="text"),)
    assert (transcript.has_language, transcript.has_segment_timestamps) == (True, True)
    assert transcript.has_word_timestamps is True
    assert transcript.is_empty is False


@pytest.mark.parametrize(
    ("status", "error_name"),
    [(400, "ModelRequestError"), (429, "ModelRateLimitError"), (503, "ModelUnavailableError")],
)
async def test_default_model_error_falls_back_to_the_second_model(
    connect: Connect, status: int, error_name: str
) -> None:
    default_failures = [error_response(status)] * (1 if status == 400 else MAX_ATTEMPTS)
    server, transport = connect([*default_failures, fixture_response("transcription_plain.json")])

    transcript = await with_fallback(transport).transcribe(CLIP)

    assert server.requests[-1].body["model"] == FALLBACK_MODEL
    assert transcript.text == SPOKEN_TEXT
    assert transcript.model == FALLBACK_MODEL
    assert transcript.attempts == (
        TranscriptionAttempt(model=DEFAULT_MODEL, outcome="error", error=error_name),
        TranscriptionAttempt(model=FALLBACK_MODEL, outcome="text"),
    )


@pytest.mark.parametrize("empty_text", ["", "   \n"])
async def test_empty_default_transcript_falls_back_to_the_second_model(
    connect: Connect, empty_text: str
) -> None:
    empty = empty_transcript_response()
    empty["text"] = empty_text
    server, transport = connect(
        [json_response(empty), fixture_response("transcription_plain.json")]
    )

    transcript = await with_fallback(transport).transcribe(CLIP)

    assert [sent.body["model"] for sent in server.requests] == [DEFAULT_MODEL, FALLBACK_MODEL]
    assert transcript.text == SPOKEN_TEXT
    assert transcript.model == FALLBACK_MODEL
    assert transcript.attempts == (
        TranscriptionAttempt(model=DEFAULT_MODEL, outcome="empty"),
        TranscriptionAttempt(model=FALLBACK_MODEL, outcome="text"),
    )


async def test_fallback_transcript_marks_language_and_timestamps_as_not_available(
    connect: Connect,
) -> None:
    _, transport = connect(
        [json_response(empty_transcript_response()), fixture_response("transcription_plain.json")]
    )

    transcript = await with_fallback(transport).transcribe(CLIP)

    assert transcript.language is None
    assert transcript.duration_seconds is None
    assert transcript.segments is None
    assert transcript.words is None
    assert (transcript.has_language, transcript.has_segment_timestamps) == (False, False)
    assert transcript.has_word_timestamps is False


async def test_both_models_hearing_nothing_is_an_empty_transcript_not_an_error(
    connect: Connect,
) -> None:
    plain_empty = load_fixture("transcription_plain.json")
    plain_empty["text"] = ""
    _, transport = connect([json_response(empty_transcript_response()), json_response(plain_empty)])

    transcript = await with_fallback(transport).transcribe(CLIP)

    assert transcript.is_empty is True
    assert transcript.model == FALLBACK_MODEL
    assert transcript.attempts == (
        TranscriptionAttempt(model=DEFAULT_MODEL, outcome="empty"),
        TranscriptionAttempt(model=FALLBACK_MODEL, outcome="empty"),
    )


async def test_both_models_failing_raises_an_error_that_lists_both_attempts(
    connect: Connect,
) -> None:
    _, transport = connect([error_response(400), error_response(401)])

    with pytest.raises(TranscriptionFailedError) as raised:
        await with_fallback(transport).transcribe(CLIP)

    assert raised.value.attempts == (
        TranscriptionAttempt(model=DEFAULT_MODEL, outcome="error", error="ModelRequestError"),
        TranscriptionAttempt(
            model=FALLBACK_MODEL, outcome="error", error="ModelAuthenticationError"
        ),
    )
    assert raised.value.operation == "transcribe"
    assert DEFAULT_MODEL in str(raised.value)
    assert FALLBACK_MODEL in str(raised.value)


async def test_empty_default_and_failed_fallback_returns_the_empty_transcript_with_the_failure(
    connect: Connect,
) -> None:
    _, transport = connect([json_response(empty_transcript_response()), error_response(400)])

    transcript = await with_fallback(transport).transcribe(CLIP)

    assert transcript.is_empty is True
    assert transcript.model == DEFAULT_MODEL
    assert transcript.language == "en"
    assert transcript.segments == ()
    assert transcript.attempts == (
        TranscriptionAttempt(model=DEFAULT_MODEL, outcome="empty"),
        TranscriptionAttempt(model=FALLBACK_MODEL, outcome="error", error="ModelRequestError"),
    )


def response_without_text() -> dict[str, object]:
    body = load_fixture("transcription_verbose.json")
    del body["text"]
    return body


async def test_response_without_text_is_a_failure_not_an_empty_transcript(
    connect: Connect,
) -> None:
    _, transport = connect([json_response(response_without_text())])

    with pytest.raises(ModelResponseError, match="has no text"):
        await default_transcriber(transport).transcribe(CLIP)


async def test_response_without_text_triggers_the_fallback(connect: Connect) -> None:
    _, transport = connect(
        [json_response(response_without_text()), fixture_response("transcription_plain.json")]
    )

    transcript = await with_fallback(transport).transcribe(CLIP)

    assert transcript.model == FALLBACK_MODEL
    assert transcript.attempts[0] == TranscriptionAttempt(
        model=DEFAULT_MODEL, outcome="error", error="ModelResponseError"
    )


@pytest.mark.parametrize(
    ("field", "broken_entries"),
    [
        ("segments", [{"start": 0.0, "text": "no end"}]),
        ("words", [{"word": "x", "start": "soon", "end": 1.0}]),
        ("words", ["not an object"]),
    ],
)
async def test_broken_timestamp_list_is_reported_as_not_available_and_the_text_is_kept(
    connect: Connect,
    caplog: pytest.LogCaptureFixture,
    field: str,
    broken_entries: list[object],
) -> None:
    body = load_fixture("transcription_verbose.json")
    body[field] = broken_entries
    _, transport = connect([json_response(body)])

    with caplog.at_level(logging.WARNING):
        transcript = await default_transcriber(transport).transcribe(CLIP)

    assert transcript.text == SPOKEN_TEXT
    assert getattr(transcript, field) is None
    warning = next(r for r in caplog.records if r.getMessage() == "transcript_metadata_invalid")
    assert warning.__dict__["field"] == field


async def test_transcript_text_and_audio_never_reach_the_logs_or_a_repr(
    connect: Connect, caplog: pytest.LogCaptureFixture
) -> None:
    _, transport = connect(
        [json_response(empty_transcript_response()), fixture_response("transcription_plain.json")]
    )

    with caplog.at_level(logging.DEBUG):
        transcript = await with_fallback(transport).transcribe(CLIP)

    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert "transcription_fallback_started" in logged
    for private in (SPOKEN_TEXT, "Example", "YXVkaW8tYnl0ZXM=", "audio-bytes"):
        assert private not in logged
        assert private not in repr(transcript)
        assert private not in repr(CLIP)


async def test_fallback_policy_works_with_any_two_transcribers() -> None:
    spoken = Transcript(
        text=SPOKEN_TEXT,
        model=FALLBACK_MODEL,
        attempts=(TranscriptionAttempt(model=FALLBACK_MODEL, outcome="text"),),
    )
    unavailable = ModelUnavailableError(
        "transcribe failed", operation="transcribe", model=DEFAULT_MODEL, status=503
    )
    primary = FakeTranscriber([unavailable])
    fallback = FakeTranscriber([spoken])

    transcript = await FallbackTranscriber(primary=primary, fallback=fallback).transcribe(CLIP)

    assert primary.calls == [CLIP]
    assert fallback.calls == [CLIP]
    assert transcript.text == SPOKEN_TEXT
    assert transcript.attempts[0] == TranscriptionAttempt(
        model=DEFAULT_MODEL, outcome="error", error="ModelUnavailableError"
    )


async def test_transcripts_that_list_no_attempts_still_get_one_attempt_per_model() -> None:
    primary = FakeTranscriber([Transcript(text="", model=DEFAULT_MODEL)])
    fallback = FakeTranscriber([Transcript(text=SPOKEN_TEXT, model=FALLBACK_MODEL)])

    transcript = await FallbackTranscriber(primary=primary, fallback=fallback).transcribe(CLIP)

    assert transcript.attempts == (
        TranscriptionAttempt(model=DEFAULT_MODEL, outcome="empty"),
        TranscriptionAttempt(model=FALLBACK_MODEL, outcome="text"),
    )
