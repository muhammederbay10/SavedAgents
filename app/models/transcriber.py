"""Speech-to-text clients: one model, and a default model with a fallback.

`OpenRouterTranscriber` asks one model (OpenRouter docs, "Speech to text",
17 Nov 2026): `POST /audio/transcriptions` with `model`, `input_audio.data`
(base64), `input_audio.format` and `response_format`. With `verbose_json` the
answer can also hold `language`, `duration`, `segments` and `words`; with plain
`json` it holds only `text`. Some models reject `verbose_json` with HTTP 400,
so the format is a setting per model.

`FallbackTranscriber` applies the owner's rule (SAV-7): ask the default model,
and ask the fallback when the default fails or returns no words. There is no
routing by language.

Metadata a model does not return is `None` on the transcript, never invented.
The fallback model returns no language and no timestamps, so a transcript it
wrote says so.
"""

import logging
from collections.abc import Callable
from dataclasses import replace

from app.models.base import (
    Audio,
    Transcriber,
    Transcript,
    TranscriptionAttempt,
    TranscriptionOutcome,
    TranscriptSegment,
    TranscriptWord,
)
from app.models.errors import ModelError, ModelResponseError, TranscriptionFailedError
from app.models.openrouter import ModelCall, OpenRouterTransport
from app.models.wire import (
    JsonObject,
    audio_input,
    cost_usd,
    list_or_none,
    number_or_none,
    object_or_none,
    string_or_none,
)

logger = logging.getLogger(__name__)

_OPERATION = "transcribe"
_VERBOSE_FORMAT = "verbose_json"
_TIMESTAMP_GRANULARITIES = ("word", "segment")


class OpenRouterTranscriber(Transcriber):
    """Transcribes with one model."""

    def __init__(
        self,
        transport: OpenRouterTransport,
        *,
        url: str,
        model: str,
        response_format: str,
        timeout_seconds: float,
    ) -> None:
        """Create the client.

        Args:
            transport: The shared OpenRouter transport.
            url: The full URL of the transcriptions endpoint.
            model: The speech-to-text model's id.
            response_format: `verbose_json` for models that return language and
                timestamps, `json` for models that return text only.
            timeout_seconds: How long one attempt may take.
        """
        self._transport = transport
        self._response_format = response_format
        self._call = ModelCall(
            operation=_OPERATION, model=model, url=url, timeout_seconds=timeout_seconds
        )

    async def transcribe(self, audio: Audio) -> Transcript:
        """Transcribe one audio clip, in whatever language is spoken.

        Args:
            audio: The clip.

        Returns:
            The transcript, with this model as its only attempt. Its text is
            empty when no speech was heard.

        Raises:
            ModelError: If the call failed or the answer had no `text`.
        """
        body = await self._transport.post(self._call, self._payload(audio))
        text = string_or_none(body.get("text"))
        if text is None:
            raise ModelResponseError(
                f"{_OPERATION} with {self._call.model} failed: the response has no text",
                operation=_OPERATION,
                model=self._call.model,
            )
        outcome: TranscriptionOutcome = "text" if text.strip() else "empty"
        return Transcript(
            text=text,
            model=self._call.model,
            language=string_or_none(body.get("language")),
            duration_seconds=number_or_none(body.get("duration")),
            segments=self._timestamps(body, "segments", _segment),
            words=self._timestamps(body, "words", _word),
            cost_usd=cost_usd(body),
            attempts=(TranscriptionAttempt(model=self._call.model, outcome=outcome),),
        )

    def _payload(self, audio: Audio) -> JsonObject:
        payload: JsonObject = {
            "model": self._call.model,
            "input_audio": audio_input(audio),
            "response_format": self._response_format,
        }
        if self._response_format == _VERBOSE_FORMAT:
            payload["timestamp_granularities"] = list(_TIMESTAMP_GRANULARITIES)
        return payload

    def _timestamps[Entry](
        self, body: JsonObject, field_name: str, parse: Callable[[JsonObject], Entry | None]
    ) -> tuple[Entry, ...] | None:
        raw_entries = list_or_none(body.get(field_name))
        if raw_entries is None:
            return None
        entries: list[Entry] = []
        for raw_entry in raw_entries:
            entry = parse(object_or_none(raw_entry) or {})
            if entry is None:
                # The text is still good. The broken list is reported as not available.
                logger.warning(
                    "transcript_metadata_invalid",
                    extra={"model": self._call.model, "field": field_name},
                )
                return None
            entries.append(entry)
        return tuple(entries)


class FallbackTranscriber(Transcriber):
    """Asks a default model, and a fallback when the default fails or hears nothing."""

    def __init__(self, primary: Transcriber, fallback: Transcriber) -> None:
        """Create the transcriber.

        Args:
            primary: The default model's transcriber.
            fallback: The transcriber asked after an error or an empty transcript.
        """
        self._primary = primary
        self._fallback = fallback

    async def transcribe(self, audio: Audio) -> Transcript:
        """Transcribe one clip with the default model, then the fallback if needed.

        The result's `attempts` lists every model that was asked:

        - The default model returned words: its transcript, one attempt.
        - The default failed or returned no words, and the fallback answered:
          the fallback's transcript (which can be empty too), two attempts.
        - The default returned no words and the fallback failed: the default's
          empty transcript, with the fallback's error as the second attempt.

        Args:
            audio: The clip.

        Returns:
            The transcript. Its `model` names the model whose answer it is.

        Raises:
            TranscriptionFailedError: If both models failed.
        """
        primary = await self._ask_primary(audio)
        if isinstance(primary, Transcript) and not primary.is_empty:
            return primary
        first_attempts = (
            _attempts_of(primary)
            if isinstance(primary, Transcript)
            else (_failed_attempt(primary),)
        )
        logger.info(
            "transcription_fallback_started",
            extra={"model": first_attempts[-1].model, "reason": first_attempts[-1].outcome},
        )
        try:
            fallback = await self._fallback.transcribe(audio)
        except ModelError as error:
            attempts = (*first_attempts, _failed_attempt(error))
            if isinstance(primary, Transcript):
                return replace(primary, attempts=attempts)
            raise TranscriptionFailedError(model=error.model, attempts=attempts) from error
        return replace(fallback, attempts=(*first_attempts, *_attempts_of(fallback)))

    async def _ask_primary(self, audio: Audio) -> Transcript | ModelError:
        try:
            return await self._primary.transcribe(audio)
        except ModelError as error:
            return error


def _attempts_of(transcript: Transcript) -> tuple[TranscriptionAttempt, ...]:
    # A transcriber that is not one of ours may leave `attempts` empty.
    if transcript.attempts:
        return transcript.attempts
    outcome: TranscriptionOutcome = "empty" if transcript.is_empty else "text"
    return (TranscriptionAttempt(model=transcript.model, outcome=outcome),)


def _failed_attempt(error: ModelError) -> TranscriptionAttempt:
    return TranscriptionAttempt(model=error.model, outcome="error", error=type(error).__name__)


def _segment(entry: JsonObject) -> TranscriptSegment | None:
    start = number_or_none(entry.get("start"))
    end = number_or_none(entry.get("end"))
    text = string_or_none(entry.get("text"))
    if start is None or end is None or text is None:
        return None
    return TranscriptSegment(start_seconds=start, end_seconds=end, text=text)


def _word(entry: JsonObject) -> TranscriptWord | None:
    start = number_or_none(entry.get("start"))
    end = number_or_none(entry.get("end"))
    word = string_or_none(entry.get("word"))
    if start is None or end is None or word is None:
        return None
    return TranscriptWord(start_seconds=start, end_seconds=end, word=word)
