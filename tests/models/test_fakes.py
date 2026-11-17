import math

import pytest

from app.models.base import (
    Audio,
    Classifier,
    Embedder,
    GenerationRequest,
    GenerationResult,
    Generator,
    Message,
    Transcriber,
    Transcript,
    Unanswered,
    YesNoAnswer,
    YesNoQuestion,
)
from app.models.errors import ModelUnavailableError
from app.models.fakes import (
    FakeClassifier,
    FakeEmbedder,
    FakeGenerator,
    FakeScriptExhaustedError,
    FakeTranscriber,
)

DIMENSIONS = 8
CLIP = Audio(data=b"audio-bytes", format="wav")
REQUEST = GenerationRequest(messages=[Message(role="user", content="a question")])
REPLY = GenerationResult(
    text="an answer", structured=None, tool_calls=(), finish_reason="stop", model="fake/text"
)
IS_LIST = YesNoQuestion(key="is_list", instructions="Is this a list?")
OUTAGE = ModelUnavailableError("down", operation="generate", model="fake/text", status=503)


def use_through_interfaces(
    classifier: Classifier, transcriber: Transcriber, generator: Generator, embedder: Embedder
) -> None:
    """Type-checks only: every fake is accepted where its interface is expected."""


def test_every_interface_has_a_fake() -> None:
    use_through_interfaces(
        FakeClassifier(), FakeTranscriber([]), FakeGenerator([]), FakeEmbedder(DIMENSIONS)
    )


async def test_fake_classifier_answers_scripted_keys_and_records_the_call() -> None:
    scripted = YesNoAnswer(probability_yes=0.9, confidence=0.9)
    classifier = FakeClassifier({"is_list": scripted})
    other = YesNoQuestion(key="other", instructions="Something else?")

    result = await classifier.decide(["a caption"], [IS_LIST, other])

    assert result.answers == {"is_list": scripted, "other": Unanswered(reason="missing")}
    assert classifier.calls == [(("a caption",), (IS_LIST, other))]


async def test_fake_classifier_raises_its_scripted_error() -> None:
    classifier = FakeClassifier(error=OUTAGE)

    with pytest.raises(ModelUnavailableError):
        await classifier.decide(["a caption"], [IS_LIST])

    assert len(classifier.calls) == 1


async def test_fake_transcriber_returns_scripted_results_in_order() -> None:
    first = Transcript(text="first", model="fake/stt")
    transcriber = FakeTranscriber([first, OUTAGE])

    assert await transcriber.transcribe(CLIP) is first
    with pytest.raises(ModelUnavailableError):
        await transcriber.transcribe(CLIP)
    assert transcriber.calls == [CLIP, CLIP]


async def test_fake_generator_returns_scripted_replies_and_records_requests() -> None:
    generator = FakeGenerator([REPLY, OUTAGE])

    assert await generator.generate(REQUEST) is REPLY
    with pytest.raises(ModelUnavailableError):
        await generator.generate(REQUEST)
    assert generator.requests == [REQUEST, REQUEST]


async def test_fake_called_more_often_than_scripted_fails_loudly() -> None:
    with pytest.raises(FakeScriptExhaustedError, match="FakeGenerator"):
        await FakeGenerator([]).generate(REQUEST)
    with pytest.raises(FakeScriptExhaustedError, match="FakeTranscriber"):
        await FakeTranscriber([]).transcribe(CLIP)


async def test_fake_embedder_returns_stable_unit_vectors_of_the_asked_size() -> None:
    embedder = FakeEmbedder(DIMENSIONS)

    first = await embedder.embed(["a cosy café", "a horror movie"])
    second = await embedder.embed(["a cosy café"])

    assert [len(vector) for vector in first.vectors] == [DIMENSIONS, DIMENSIONS]
    assert first.vectors[0] == second.vectors[0]
    assert first.vectors[0] != first.vectors[1]
    for vector in first.vectors:
        assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0)
    assert embedder.calls == [("a cosy café", "a horror movie"), ("a cosy café",)]


async def test_fake_embedder_refuses_an_empty_batch_and_raises_its_scripted_error() -> None:
    with pytest.raises(ValueError, match="at least one text"):
        await FakeEmbedder(DIMENSIONS).embed([])
    with pytest.raises(ModelUnavailableError):
        await FakeEmbedder(DIMENSIONS, error=OUTAGE).embed(["a text"])
