"""Fake implementations of every provider interface, for tests.

They make no network calls and cost nothing. Each one records the calls it
received, and returns what the test scripted. A scripted `ModelError` is raised
instead of returned, so failure paths can be tested the same way.

    generator = FakeGenerator([GenerationResult(text="Hello", ...)])
    reply = await generator.generate(request)
    assert generator.requests == [request]
"""

import hashlib
import math
from collections.abc import Mapping, Sequence

from app.models.base import (
    Answer,
    Audio,
    Classifier,
    ContentPart,
    DecisionResult,
    Embedder,
    EmbeddingResult,
    GenerationRequest,
    GenerationResult,
    Generator,
    Question,
    Transcriber,
    Transcript,
    Unanswered,
)
from app.models.errors import ModelError

FAKE_DECISION_MODEL = "fake/decision"
FAKE_EMBEDDING_MODEL = "fake/embedding"

_BYTE_MIDPOINT = 127.5


class FakeScriptExhaustedError(Exception):
    """A fake was called more often than the test scripted results for."""


class FakeClassifier(Classifier):
    """Returns scripted answers by question key.

    Attributes:
        calls: The `(state, questions)` of every call, in order.
    """

    def __init__(
        self, answers: Mapping[str, Answer] | None = None, *, error: ModelError | None = None
    ) -> None:
        """Create the fake.

        Args:
            answers: The answer to give for each question key. A question whose
                key is not listed comes back as `Unanswered("missing")`.
            error: When set, every call raises it.
        """
        self._answers = dict(answers or {})
        self._error = error
        self.calls: list[tuple[tuple[ContentPart, ...], tuple[Question, ...]]] = []

    async def decide(
        self, state: Sequence[ContentPart], questions: Sequence[Question]
    ) -> DecisionResult:
        """Record the call and return the scripted answers.

        Args:
            state: What the questions are about.
            questions: The questions.

        Returns:
            The scripted answer for every question asked.

        Raises:
            ModelError: The scripted error, if one was given.
        """
        self.calls.append((tuple(state), tuple(questions)))
        if self._error is not None:
            raise self._error
        return DecisionResult(
            answers={
                question.key: self._answers.get(question.key, Unanswered(reason="missing"))
                for question in questions
            },
            model=FAKE_DECISION_MODEL,
            cost_usd=None,
        )


class FakeTranscriber(Transcriber):
    """Returns scripted transcripts, one per call.

    Attributes:
        calls: The audio clip of every call, in order.
    """

    def __init__(self, results: Sequence[Transcript | ModelError]) -> None:
        """Create the fake.

        Args:
            results: What each call returns or raises, in order.
        """
        self._results = list(results)
        self.calls: list[Audio] = []

    async def transcribe(self, audio: Audio) -> Transcript:
        """Record the call and return the next scripted transcript.

        Args:
            audio: The clip.

        Returns:
            The next scripted transcript.

        Raises:
            ModelError: The next scripted result, if it is an error.
            FakeScriptExhaustedError: If no scripted result is left.
        """
        self.calls.append(audio)
        return _next_result(self._results, "FakeTranscriber")


class FakeGenerator(Generator):
    """Returns scripted replies, one per call.

    Attributes:
        requests: The request of every call, in order.
    """

    def __init__(self, results: Sequence[GenerationResult | ModelError]) -> None:
        """Create the fake.

        Args:
            results: What each call returns or raises, in order.
        """
        self._results = list(results)
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Record the request and return the next scripted reply.

        Args:
            request: The request.

        Returns:
            The next scripted reply.

        Raises:
            ModelError: The next scripted result, if it is an error.
            FakeScriptExhaustedError: If no scripted result is left.
        """
        self.requests.append(request)
        return _next_result(self._results, "FakeGenerator")


class FakeEmbedder(Embedder):
    """Returns a stable unit vector for each text, with no model behind it.

    The same text always gets the same vector, and different texts get
    different ones. The vectors carry no meaning: similar texts are not close.

    Attributes:
        calls: The texts of every call, in order.
    """

    def __init__(self, dimensions: int, *, error: ModelError | None = None) -> None:
        """Create the fake.

        Args:
            dimensions: The size of the vectors to return.
            error: When set, every call raises it.
        """
        self._dimensions = dimensions
        self._error = error
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """Record the call and return one stable vector per text.

        Args:
            texts: The texts. At least one.

        Returns:
            One unit vector per text, in the same order.

        Raises:
            ValueError: If `texts` is empty.
            ModelError: The scripted error, if one was given.
        """
        if not texts:
            raise ValueError("embed needs at least one text")
        self.calls.append(tuple(texts))
        if self._error is not None:
            raise self._error
        return EmbeddingResult(
            vectors=tuple(self._vector(text) for text in texts),
            model=FAKE_EMBEDDING_MODEL,
            cost_usd=None,
        )

    def _vector(self, text: str) -> tuple[float, ...]:
        stream = hashlib.shake_256(text.encode("utf-8")).digest(self._dimensions)
        centred = [byte - _BYTE_MIDPOINT for byte in stream]
        length = math.sqrt(sum(value * value for value in centred))
        return tuple(value / length for value in centred)


def _next_result[Result](results: list[Result | ModelError], fake_name: str) -> Result:
    if not results:
        raise FakeScriptExhaustedError(f"{fake_name} was called more often than it was scripted")
    result = results.pop(0)
    if isinstance(result, ModelError):
        raise result
    return result
