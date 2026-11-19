"""Step 3, speech-to-text: routed by the audio track and the transcript, never by a model's opinion.

The owner's rule (SAV-7, decision record 0008, `docs/ARCHITECTURE.md` §3.3):

- No media, or media with no audio track: no speech, and speech-to-text is skipped.
- An audio track: speech-to-text always runs, through the default model with
  the fallback model on an error or an empty answer (`FallbackTranscriber`).
  A caption that already names the thing does not skip it, and no model's
  "has speech" answer is asked for or used.
- The final transcript is **near-empty** when it has fewer letters and digits
  than `TRANSCRIPT_MIN_SPEECH_CHARACTERS` (see `count_speech_characters`). An
  empty or near-empty transcript means no speech.
- A failure is not silence. When no model produced a transcript, or the
  default model heard nothing and the fallback then failed, the step raises
  and the job is tried again; the item is not marked as having no speech.
"""

import asyncio
import tempfile
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from app.models.base import Audio, Transcriber, Transcript
from app.models.errors import ModelError
from app.pipeline.costs import CallCosts
from app.pipeline.fetch import StoredMedia
from app.pipeline.media import AUDIO_FORMAT, MediaTools
from app.pipeline.storage import MediaStore

# Unicode general categories that count as speech: letters (L), digits and
# other numbers (N), and the marks that combine with letters (M), which many
# scripts need to write a syllable. Punctuation, symbols and spaces don't count.
_SPEECH_CATEGORY_CLASSES: Final = frozenset({"L", "N", "M"})
_VIDEO_FILE_NAME: Final = "video.mp4"
_ERROR_OUTCOME: Final = "error"


class SpeechOutcome(StrEnum):
    """How the step decided whether the item has speech."""

    NO_MEDIA = "no_media"
    NO_AUDIO_TRACK = "no_audio_track"
    NO_SPEECH = "no_speech"
    SPEECH = "speech"


class IncompleteTranscriptionError(ModelError):
    """The default model heard no words and the fallback model then failed.

    The transcript is not trusted as "no speech", because the fallback the
    owner's rule asks for did not answer. The job is tried again.
    """


def count_speech_characters(text: str) -> int:
    """Count the characters of a transcript that carry speech, in any script.

    Letters, digits and combining marks count; spaces, punctuation and symbols
    such as "♪" don't. Words are not counted, because many languages don't
    separate words with spaces.

    Args:
        text: The transcript.

    Returns:
        How many of its characters are letters, digits or combining marks.
    """
    return sum(
        1 for character in text if unicodedata.category(character)[0] in _SPEECH_CATEGORY_CLASSES
    )


@dataclass(frozen=True)
class SpeechResult:
    """What speech-to-text found.

    Attributes:
        outcome: How the decision was made.
        transcript: The final transcript when speech-to-text ran; `None` when skipped.
        costs: The charges of the speech-to-text calls.
    """

    outcome: SpeechOutcome
    transcript: Transcript | None = field(default=None, repr=False)
    costs: CallCosts = field(default_factory=CallCosts)

    @property
    def has_speech(self) -> bool:
        """Return whether the item has speech."""
        return self.outcome is SpeechOutcome.SPEECH

    @property
    def speech_text(self) -> str | None:
        """Return the transcript to store on the item, or `None` without speech."""
        if not self.has_speech or self.transcript is None:
            return None
        return self.transcript.text.strip()

    def to_output(self) -> dict[str, object]:
        """Return the step's output for `enrichment_run.output`.

        The transcript's text is stored on the saved item, not here. Language
        and timestamps are kept; `None` means the answering model did not
        return them.

        Returns:
            A JSON object.
        """
        transcript = self.transcript
        return {
            "outcome": self.outcome,
            "has_speech": self.has_speech,
            "model": transcript.model if transcript else None,
            "language": transcript.language if transcript else None,
            "duration_seconds": transcript.duration_seconds if transcript else None,
            "segments": _segments(transcript),
            "words": _words(transcript),
            "attempts": [
                {"model": attempt.model, "outcome": attempt.outcome, "error": attempt.error}
                for attempt in (transcript.attempts if transcript else ())
            ],
            "costs": self.costs.to_output(),
        }


class SpeechToText:
    """Decides whether an item has speech, and transcribes it when it has an audio track."""

    def __init__(
        self,
        transcriber: Transcriber,
        tools: MediaTools,
        store: MediaStore,
        *,
        min_speech_characters: int,
    ) -> None:
        """Create the step.

        Args:
            transcriber: Speech-to-text with its fallback.
            tools: ffmpeg, to take the audio out of the stored video.
            store: Where the video is kept.
            min_speech_characters: The near-empty rule's threshold.
        """
        self._transcriber = transcriber
        self._tools = tools
        self._store = store
        self._min_speech_characters = min_speech_characters

    async def transcribe(self, media: StoredMedia) -> SpeechResult:
        """Transcribe an item's audio track, if it has one.

        Args:
            media: What the fetch step kept.

        Returns:
            Whether the item has speech, with the transcript when one was made.

        Raises:
            TranscriptionFailedError: If no model produced a transcript.
            IncompleteTranscriptionError: If the default model heard nothing
                and the fallback failed.
            MediaToolError: If the audio could not be taken out of the video.
            MediaNotFoundError: If the stored video is gone.
        """
        if media.kind is None or media.media_key is None:
            return SpeechResult(outcome=SpeechOutcome.NO_MEDIA)
        if not media.has_audio:
            return SpeechResult(outcome=SpeechOutcome.NO_AUDIO_TRACK)
        audio = await self._audio_of(media.media_key)
        transcript = await self._transcriber.transcribe(Audio(data=audio, format=AUDIO_FORMAT))
        costs = _transcription_costs(transcript)
        if transcript.attempts and transcript.attempts[-1].outcome == _ERROR_OUTCOME:
            raise IncompleteTranscriptionError(
                "transcribe found no words with the default model and the fallback failed",
                operation="transcribe",
                model=transcript.attempts[-1].model,
            )
        speech = count_speech_characters(transcript.text) >= self._min_speech_characters
        outcome = SpeechOutcome.SPEECH if speech else SpeechOutcome.NO_SPEECH
        return SpeechResult(outcome=outcome, transcript=transcript, costs=costs)

    async def _audio_of(self, media_key: str) -> bytes:
        video = await self._store.get(media_key)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / _VIDEO_FILE_NAME
            await asyncio.to_thread(path.write_bytes, video)
            return await self._tools.extract_audio(path)


def _transcription_costs(transcript: Transcript) -> CallCosts:
    # The transcript reports the charge of the model whose answer it is. A
    # model asked before it (the default, when the fallback answered) reported
    # its charge to nobody, so that call is counted as unpriced.
    calls = max(len(transcript.attempts), 1)
    priced = 0 if transcript.cost_usd is None else 1
    return CallCosts(
        cost_usd=transcript.cost_usd or 0.0, calls=calls, unpriced_calls=calls - priced
    )


def _segments(transcript: Transcript | None) -> list[dict[str, object]] | None:
    if transcript is None or transcript.segments is None:
        return None
    return [
        {
            "start_seconds": segment.start_seconds,
            "end_seconds": segment.end_seconds,
            "text": segment.text,
        }
        for segment in transcript.segments
    ]


def _words(transcript: Transcript | None) -> list[dict[str, object]] | None:
    if transcript is None or transcript.words is None:
        return None
    return [
        {"start_seconds": word.start_seconds, "end_seconds": word.end_seconds, "word": word.word}
        for word in transcript.words
    ]
