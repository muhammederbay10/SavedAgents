"""Fakes and builders for the pipeline tests. Nothing here reaches the network.

All captions, transcripts, names and links are synthetic.
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.adapters.base import CaptureEvent
from app.agent.messenger import OutgoingMessage, SendResult, SendStatus
from app.models.base import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    ContentPart,
    DecisionResult,
    GenerationResult,
    Question,
    Transcript,
    TranscriptionAttempt,
    Unanswered,
    YesNoAnswer,
)
from app.pipeline.classify import IS_LIST_QUESTION_KEY, NONE_OF_THESE, TYPE_QUESTION_KEY
from app.pipeline.media import MediaProbe, MediaTools
from app.pipeline.normalize import MATCH_QUESTION_KEY, NEW_VALUE_OPTION, PRIVACY_QUESTION_KEY
from app.pipeline.prompts import PipelinePrompts

TEST_PROMPTS = PipelinePrompts(
    version="test-prompts-1",
    classify_type_question="test: which type?",
    classify_none_option="test: none of these",
    classify_is_list_question="test: is it a list?",
    classify_input="caption={caption}\ncollection={collection}",
    extraction_system="test: extract",
    extraction_input="collection={collection}\ncaption={caption}\ntranscript={transcript}",
    escalation_system="test: watch the media",
    missing_value="(none)",
    privacy_gate_question="test: is it generic?",
    vocabulary_match_question="test: same {field}?",
    vocabulary_new_option="test: new value",
    vocabulary_input="{field}\n{wording}",
)

TEXT_MODEL = "fake/text"
ESCALATION_MODEL = "fake/escalation"
DECISION_MODEL = "fake/decision"
PRIMARY_STT_MODEL = "fake/mai"
FALLBACK_STT_MODEL = "fake/gpt-transcribe"
MEDIA_URL = (
    "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=18000000000000901&signature=FAKE"
)
SHARED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
VIDEO_BYTES = b"synthetic original video"
IMAGE_BYTES = b"synthetic original image"


@dataclass
class ScriptedClassifier:
    """A decision model that answers by question, and by the wording a vocabulary question is about.

    Attributes:
        content_type: The option the type question picks.
        is_list_probability: The probability of yes for is-list.
        generic_probability: The privacy gate's probability of "generic", by wording.
        default_generic_probability: The gate's probability for other wording.
        matches: The existing value each wording is matched to; unlisted wording is new.
        unanswered: Question keys to leave unanswered.
        extra_answers: Answers to questions the pipeline should never ask,
            such as a model claiming that a clip has speech.
        cost_usd: The charge reported for each call.
        calls: The state and questions of every call.
    """

    content_type: str = NONE_OF_THESE
    is_list_probability: float = 0.1
    generic_probability: dict[str, float] = field(default_factory=dict)
    default_generic_probability: float = 0.99
    matches: dict[str, str] = field(default_factory=dict)
    unanswered: set[str] = field(default_factory=set)
    extra_answers: dict[str, Answer] = field(default_factory=dict)
    cost_usd: float | None = 0.0001
    calls: list[tuple[tuple[ContentPart, ...], tuple[Question, ...]]] = field(default_factory=list)

    async def decide(
        self, state: Sequence[ContentPart], questions: Sequence[Question]
    ) -> DecisionResult:
        self.calls.append((tuple(state), tuple(questions)))
        return DecisionResult(
            answers={question.key: self._answer(state, question) for question in questions},
            model=DECISION_MODEL,
            cost_usd=self.cost_usd,
        )

    def asked_keys(self) -> list[str]:
        return [question.key for _, questions in self.calls for question in questions]

    def _answer(self, state: Sequence[ContentPart], question: Question) -> Answer:
        if question.key in self.unanswered:
            return Unanswered(reason="refused")
        if question.key == TYPE_QUESTION_KEY:
            return ChoiceAnswer(
                choice=self.content_type,
                probabilities={self.content_type: 0.9},
                confidence=0.9,
            )
        if question.key == IS_LIST_QUESTION_KEY:
            return YesNoAnswer(probability_yes=self.is_list_probability, confidence=None)
        wording = _wording(state)
        if question.key == PRIVACY_QUESTION_KEY:
            probability = self.generic_probability.get(wording, self.default_generic_probability)
            return YesNoAnswer(probability_yes=probability, confidence=None)
        if question.key == MATCH_QUESTION_KEY and isinstance(question, ChoiceQuestion):
            wanted = self.matches.get(wording)
            for key, description in question.options.items():
                if description == wanted:
                    return ChoiceAnswer(choice=key, probabilities={key: 0.95}, confidence=None)
            return ChoiceAnswer(
                choice=NEW_VALUE_OPTION, probabilities={NEW_VALUE_OPTION: 0.9}, confidence=None
            )
        return self.extra_answers.get(question.key, Unanswered(reason="missing"))


def _wording(state: Sequence[ContentPart]) -> str:
    text = next(part for part in state if isinstance(part, str))
    return text.split("\n", 1)[1]


@dataclass
class FakeMediaTools(MediaTools):
    """ffprobe and ffmpeg stand-ins that write small synthetic files.

    Attributes:
        probe_result: What `probe` reports for every file.
        calls: The name of every method called, in order.
    """

    probe_result: MediaProbe = field(
        default_factory=lambda: MediaProbe(has_video=True, has_audio=True, duration_seconds=12.5)
    )
    calls: list[str] = field(default_factory=list)

    async def probe(self, source: Path) -> MediaProbe:
        self.calls.append("probe")
        return self.probe_result

    async def make_video_copy(self, source: Path, target: Path, max_height: int) -> None:
        self.calls.append("make_video_copy")
        await asyncio.to_thread(target.write_bytes, b"video copy " + str(max_height).encode())

    async def make_still(self, source: Path, target: Path, max_height: int) -> None:
        self.calls.append("make_still")
        await asyncio.to_thread(target.write_bytes, b"still " + str(max_height).encode())

    async def extract_audio(self, source: Path) -> bytes:
        self.calls.append("extract_audio")
        return b"synthetic wav"


@dataclass
class RecordingMessenger:
    """Records the DMs it is asked to send.

    Attributes:
        sent: `(recipient, text)` of every message.
    """

    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send(
        self, recipient_id: str, message: OutgoingMessage, last_user_message_at: datetime
    ) -> SendResult:
        self.sent.append((recipient_id, message.text))
        return SendResult(status=SendStatus.SENT, message_id="synthetic-mid", attempts=1)


class MediaServer:
    """Serves one media file to `httpx`, and counts the downloads."""

    def __init__(self, content: bytes, content_type: str, status: int = 200) -> None:
        self.content = content
        self.content_type = content_type
        self.status = status
        self.requests = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(
            self.status, content=self.content, headers={"content-type": self.content_type}
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def structured_reply(
    document: Mapping[str, object], *, model: str = TEXT_MODEL, cost_usd: float | None = 0.0002
) -> GenerationResult:
    return GenerationResult(
        text="",
        structured=dict(document),
        tool_calls=(),
        finish_reason="stop",
        model=model,
        cost_usd=cost_usd,
    )


def transcript(
    text: str,
    *,
    model: str = PRIMARY_STT_MODEL,
    attempts: Sequence[TranscriptionAttempt] = (),
    cost_usd: float | None = 0.0003,
) -> Transcript:
    return Transcript(
        text=text,
        model=model,
        language="und",
        cost_usd=cost_usd,
        attempts=tuple(attempts),
    )


def capture_event(
    user_id: uuid.UUID,
    *,
    media_url: str | None = MEDIA_URL,
    caption: str | None = None,
    media_type: str = "reel",
) -> CaptureEvent:
    return CaptureEvent.model_validate(
        {
            "user_id": user_id,
            "platform": "instagram",
            "source": "dm",
            "post_url": None,
            "platform_post_id": None,
            "caption": caption,
            "media_url": media_url,
            "media_type": media_type,
            "collection_hint": None,
            "source_message_id": f"synthetic-mid-{uuid.uuid4().hex}",
            "received_at": SHARED_AT,
        }
    )


def place_extraction(
    *names_and_cuisines: tuple[str, str | None],
    category: str = "Restaurant",
    tags: Sequence[str] = ("Rooftop",),
) -> dict[str, object]:
    return {
        "summary": "A synthetic summary of a place post.",
        "category": category,
        "tags": list(tags),
        "entities": [
            {"name": name, "area_hint": None, "cuisine": cuisine}
            for name, cuisine in names_and_cuisines
        ],
    }


def movie_extraction(*titles: str) -> dict[str, object]:
    return {
        "summary": "A synthetic list of movies.",
        "category": "movie list",
        "tags": ["weekend"],
        "entities": [{"title": title, "year_hint": None} for title in titles],
    }


def study_extraction() -> dict[str, object]:
    return {
        "summary": "A synthetic tip about spaced repetition.",
        "category": "study tip",
        "tags": ["memory"],
        "subject": "Study Skills",
        "topic": "spaced repetition",
        "format": "Tip",
        "key_points": ["Review just before you forget."],
        "resources": [],
        "exam": None,
    }


def open_extraction(
    category: str = "Car Review", tags: Sequence[str] = ("Sports Car",)
) -> dict[str, object]:
    return {
        "summary": "A synthetic review of a car.",
        "category": category,
        "tags": list(tags),
    }
