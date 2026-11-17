"""The provider interfaces every model call goes through, and their value types.

Four small interfaces cover every model role (ARCHITECTURE §3.9):

- `Classifier.decide`: typed questions about some content, answered with probabilities.
- `Transcriber.transcribe`: speech to text.
- `Generator.generate`: free text, JSON that follows a schema, and tool calls.
- `Embedder.embed`: texts to vectors.

Pipeline and agent code depends on these interfaces only, so a model is swapped
in config without touching its callers. Fields that can hold a user's caption,
transcript, message or media are left out of `repr`, so a value that ends up in
a log line or an error message does not carry private content with it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]
ToolChoice = Literal["auto", "none", "required"]
UnansweredReason = Literal["refused", "missing", "invalid"]
TranscriptionOutcome = Literal["text", "empty", "error"]


@dataclass(frozen=True)
class Image:
    """An image to show a model.

    Attributes:
        data: The file's bytes.
        mime_type: For example `image/jpeg`.
    """

    data: bytes = field(repr=False)
    mime_type: str


@dataclass(frozen=True)
class Video:
    """A video to show a model.

    Attributes:
        data: The file's bytes.
        mime_type: For example `video/mp4`.
    """

    data: bytes = field(repr=False)
    mime_type: str


@dataclass(frozen=True)
class Audio:
    """An audio clip to play to a model.

    Attributes:
        data: The file's bytes.
        format: The container format as the provider names it, for example
            `wav`, `mp3` or `m4a`.
    """

    data: bytes = field(repr=False)
    format: str


Media = Image | Video | Audio
ContentPart = str | Media


@dataclass(frozen=True)
class YesNoQuestion:
    """A question answered with the probability that the answer is yes.

    Attributes:
        key: Identifies the question in the result.
        instructions: The question, in plain words.
    """

    key: str
    instructions: str = field(repr=False)


@dataclass(frozen=True)
class ChoiceQuestion:
    """A question answered with exactly one of the given options.

    Attributes:
        key: Identifies the question in the result.
        instructions: The question, in plain words.
        options: Each option's key, mapped to a description of when it applies.
    """

    key: str
    instructions: str = field(repr=False)
    options: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class ScoreQuestion:
    """A question answered with a position on an ordered scale.

    Attributes:
        key: Identifies the question in the result.
        instructions: The question, in plain words.
        levels: Descriptions of the scale's levels, from the lowest to the
            highest. The answer's score runs from 0 to `len(levels) - 1`.
    """

    key: str
    instructions: str = field(repr=False)
    levels: Sequence[str] = field(repr=False)


Question = YesNoQuestion | ChoiceQuestion | ScoreQuestion


@dataclass(frozen=True)
class YesNoAnswer:
    """The answer to a `YesNoQuestion`.

    Attributes:
        probability_yes: Probability, from 0 to 1, that the answer is yes.
        confidence: How sure the model says it is, from 0 to 1; `None` if it
            did not say.
    """

    probability_yes: float
    confidence: float | None


@dataclass(frozen=True)
class ChoiceAnswer:
    """The answer to a `ChoiceQuestion`.

    Attributes:
        choice: The key of the chosen option.
        probabilities: The probability of each option, by option key.
        confidence: How sure the model says it is, from 0 to 1; `None` if it
            did not say.
    """

    choice: str
    probabilities: Mapping[str, float]
    confidence: float | None


@dataclass(frozen=True)
class ScoreAnswer:
    """The answer to a `ScoreQuestion`.

    Attributes:
        score: A position on the scale, from 0 (the first level) to the number
            of levels minus one. It can fall between two levels.
        probabilities: The probability of each level, by the level's index.
        confidence: How sure the model says it is, from 0 to 1; `None` if it
            did not say.
    """

    score: float
    probabilities: Mapping[int, float]
    confidence: float | None


@dataclass(frozen=True)
class Unanswered:
    """A question the model did not answer. The other answers are still valid.

    Attributes:
        reason: `refused` if the model declined, `missing` if the response had
            no answer for the key, `invalid` if the answer was unusable (the
            wrong type, an unknown option or a probability outside 0 to 1).
    """

    reason: UnansweredReason


Answer = YesNoAnswer | ChoiceAnswer | ScoreAnswer | Unanswered


@dataclass(frozen=True)
class DecisionResult:
    """Every answer of one `Classifier.decide` call.

    Attributes:
        answers: One entry per question asked, by question key.
        model: The model that answered, as the provider reports it. This can be
            a dated version of the configured id.
        cost_usd: What the provider charged for the call; `None` if not reported.
    """

    answers: Mapping[str, Answer]
    model: str
    cost_usd: float | None


@dataclass(frozen=True)
class TranscriptSegment:
    """A stretch of speech with its time in the clip.

    Attributes:
        start_seconds: Where the segment starts.
        end_seconds: Where the segment ends.
        text: What was said.
    """

    start_seconds: float
    end_seconds: float
    text: str = field(repr=False)


@dataclass(frozen=True)
class TranscriptWord:
    """One spoken word with its time in the clip.

    Attributes:
        start_seconds: Where the word starts.
        end_seconds: Where the word ends.
        word: The word.
    """

    start_seconds: float
    end_seconds: float
    word: str = field(repr=False)


@dataclass(frozen=True)
class TranscriptionAttempt:
    """What one model did with a clip.

    Attributes:
        model: The configured id of the model that was asked.
        outcome: `text` for a transcript with words in it, `empty` for a valid
            answer with no words, `error` if the call failed.
        error: The error's class name when the outcome is `error`.
    """

    model: str
    outcome: TranscriptionOutcome
    error: str | None = None


@dataclass(frozen=True)
class Transcript:
    """The result of `Transcriber.transcribe`.

    Language and timestamps are optional metadata. When the answering model
    does not return one of them it is `None`, which means "not available". It
    is never filled in with a guess. An empty tuple means the model did return
    the list and it had no entries.

    An empty `text` is a valid result (the clip has no speech the model could
    hear), not a failure. Failures are raised as errors.

    Attributes:
        text: The transcript. Empty when the model heard no speech.
        model: The configured id of the model whose answer this is.
        language: The language the model detected; `None` if not available.
        duration_seconds: The clip's length as the model reports it; `None` if
            not available.
        segments: Segment timestamps; `None` if not available.
        words: Word timestamps; `None` if not available.
        cost_usd: What the provider charged for this model's call; `None` if
            not reported.
        attempts: Every model that was asked, in order. A transcriber with a
            fallback lists both models here when the fallback ran.
    """

    text: str = field(repr=False)
    model: str
    language: str | None = None
    duration_seconds: float | None = None
    segments: tuple[TranscriptSegment, ...] | None = None
    words: tuple[TranscriptWord, ...] | None = None
    cost_usd: float | None = None
    attempts: tuple[TranscriptionAttempt, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Return whether the transcript has no words at all."""
        return not self.text.strip()

    @property
    def has_language(self) -> bool:
        """Return whether the model reported the spoken language."""
        return self.language is not None

    @property
    def has_segment_timestamps(self) -> bool:
        """Return whether the model returned segment timestamps."""
        return self.segments is not None

    @property
    def has_word_timestamps(self) -> bool:
        """Return whether the model returned word timestamps."""
        return self.words is not None


@dataclass(frozen=True)
class ToolDefinition:
    """A tool the model may call.

    Attributes:
        name: The tool's name.
        description: What the tool does and when to use it.
        parameters: A JSON Schema object for the tool's arguments.
    """

    name: str
    description: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class ToolCall:
    """A call to a tool that the model asked for.

    Attributes:
        id: The call's id. The tool's result is sent back under this id.
        name: The tool to call.
        arguments: The arguments, parsed from the model's JSON.
    """

    id: str
    name: str
    arguments: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True)
class Message:
    """One message of a conversation with a generator.

    Attributes:
        role: Who wrote it. `tool` is the result of a tool call.
        content: Text, or a list of text and media parts.
        tool_calls: For an `assistant` message, the tool calls it made.
        tool_call_id: For a `tool` message, the id of the call it answers.
    """

    role: Role
    content: str | Sequence[ContentPart] = field(repr=False)
    tool_calls: Sequence[ToolCall] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class OutputSchema:
    """A JSON Schema the model's reply must follow.

    Attributes:
        name: A short name for the schema.
        schema: The JSON Schema object.
    """

    name: str
    schema: Mapping[str, object]


@dataclass(frozen=True)
class GenerationRequest:
    """What to ask a generator.

    Attributes:
        messages: The conversation so far, oldest first.
        output_schema: When set, the reply must be a JSON object that follows
            this schema.
        tools: The tools the model may call.
        tool_choice: `auto` lets the model decide, `none` forbids tool calls,
            `required` forces one. Only used when `tools` is not empty.
        max_output_tokens: An upper limit on the reply's length; `None` leaves
            it to the model.
        temperature: Sampling temperature; `None` leaves it to the model.
    """

    messages: Sequence[Message]
    output_schema: OutputSchema | None = None
    tools: Sequence[ToolDefinition] = ()
    tool_choice: ToolChoice = "auto"
    max_output_tokens: int | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class GenerationResult:
    """A generator's reply.

    Attributes:
        text: The reply's text. Empty when the model only called tools.
        structured: The reply parsed as a JSON object, when the request had an
            `output_schema` and the model did not call a tool instead. The
            object is valid JSON; checking it against the schema's own model is
            the caller's job.
        tool_calls: The tool calls the model asked for.
        finish_reason: Why the model stopped: `stop`, `tool_calls`, `length`
            or `content_filter`.
        model: The model that answered, as the provider reports it.
        provider: The upstream provider that served the call; `None` if not
            reported.
        cost_usd: What the provider charged for the call; `None` if not reported.
    """

    text: str = field(repr=False)
    structured: Mapping[str, object] | None = field(repr=False)
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    model: str
    provider: str | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    """The vectors of one `Embedder.embed` call.

    Attributes:
        vectors: One vector per input text, in the order of the inputs.
        model: The model that answered, as the provider reports it.
        cost_usd: What the provider charged for the call; `None` if not reported.
    """

    vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    model: str
    cost_usd: float | None = None


class Classifier(Protocol):
    """Answers typed questions about some content, with probabilities."""

    async def decide(
        self, state: Sequence[ContentPart], questions: Sequence[Question]
    ) -> DecisionResult:
        """Answer every question about the same content in one call.

        Args:
            state: What the questions are about: text and media, in order.
            questions: The questions. Their keys must be unique.

        Returns:
            One answer per question. A question the model did not answer comes
            back as `Unanswered`.

        Raises:
            ValueError: If there are no questions or two share a key.
            ModelError: If the call failed.
        """
        ...


class Transcriber(Protocol):
    """Turns speech into text."""

    async def transcribe(self, audio: Audio) -> Transcript:
        """Transcribe one audio clip, in whatever language is spoken.

        Args:
            audio: The clip.

        Returns:
            The transcript. Its text is empty when no speech was heard.

        Raises:
            ModelError: If no transcript could be produced.
        """
        ...


class Generator(Protocol):
    """Writes text, JSON that follows a schema, and tool calls."""

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Produce the next reply of a conversation.

        Args:
            request: The messages, and optionally an output schema and tools.

        Returns:
            The model's reply.

        Raises:
            ModelError: If the call failed or the reply was unusable.
        """
        ...


class Embedder(Protocol):
    """Turns texts into vectors of one fixed size."""

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed a batch of texts in one call.

        Args:
            texts: The texts. At least one.

        Returns:
            One vector per text, in the same order.

        Raises:
            ValueError: If `texts` is empty.
            ModelError: If the call failed or a vector had the wrong size.
        """
        ...
