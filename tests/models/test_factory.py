from collections.abc import AsyncIterator

import httpx
import pytest

from app.config import ModelSettings
from app.models.base import (
    Audio,
    GenerationRequest,
    Message,
    YesNoQuestion,
)
from app.models.errors import EmbeddingDimensionError
from app.models.factory import ModelClients, build_model_clients, open_http_client
from tests.model_environment import MODEL_ENVIRONMENT
from tests.models.support import FakeOpenRouter, fixture_response, json_response, load_fixture

QUESTION = GenerationRequest(messages=[Message(role="user", content="a question")])


@pytest.fixture
def model_settings(monkeypatch: pytest.MonkeyPatch) -> ModelSettings:
    for name, value in MODEL_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return ModelSettings(_env_file=None)


@pytest.fixture
def server() -> FakeOpenRouter:
    return FakeOpenRouter([])


@pytest.fixture
async def clients(
    model_settings: ModelSettings, server: FakeOpenRouter
) -> AsyncIterator[ModelClients]:
    async with open_http_client(model_settings, httpx.MockTransport(server.handle)) as http_client:
        yield build_model_clients(model_settings, http_client)


async def test_classifier_uses_the_configured_decisions_url_and_model(
    clients: ModelClients, server: FakeOpenRouter
) -> None:
    server.script(fixture_response("decisions_response.json"))

    await clients.classifier.decide(["a caption"], [YesNoQuestion("is_list", "Is it a list?")])

    assert server.only_request.url == "https://openrouter.test/api/alpha/decisions"
    assert server.only_request.body["model"] == "google/gemini-decision-1"
    assert server.only_request.authorization == "Bearer synthetic-key-3b1f"
    assert server.only_request.timeout_seconds == 30.0


async def test_text_generator_is_pinned_to_the_configured_provider(
    clients: ModelClients, server: FakeOpenRouter
) -> None:
    server.script(fixture_response("chat_completion_schema.json"))

    await clients.text_generator.generate(QUESTION)

    assert server.only_request.url == "https://openrouter.test/api/v1/chat/completions"
    assert server.only_request.body["model"] == "deepseek/deepseek-v4.1-flash"
    assert server.only_request.body["provider"] == {
        "only": ["coreweave/fp8"],
        "order": ["coreweave/fp8"],
        "allow_fallbacks": False,
    }


async def test_escalation_generator_uses_its_own_model_and_timeout_without_a_pin(
    clients: ModelClients, server: FakeOpenRouter
) -> None:
    reply = load_fixture("chat_completion_schema.json")
    reply["provider"] = "Google"
    server.script(json_response(reply))

    await clients.escalation_generator.generate(QUESTION)

    assert server.only_request.body["model"] == "google/gemini-2.5-flash"
    assert "provider" not in server.only_request.body
    assert server.only_request.timeout_seconds == 90.0


async def test_transcriber_asks_the_default_model_then_the_fallback_model(
    clients: ModelClients, server: FakeOpenRouter
) -> None:
    empty = load_fixture("transcription_verbose.json")
    empty["text"] = ""
    server.script(json_response(empty), fixture_response("transcription_plain.json"))

    transcript = await clients.transcriber.transcribe(Audio(data=b"audio-bytes", format="wav"))

    assert [sent.url for sent in server.requests] == [
        "https://openrouter.test/api/v1/audio/transcriptions"
    ] * 2
    assert [(sent.body["model"], sent.body["response_format"]) for sent in server.requests] == [
        ("microsoft/mai-transcribe-2", "verbose_json"),
        ("openai/gpt-transcribe", "json"),
    ]
    assert transcript.model == "openai/gpt-transcribe"


async def test_embedder_asks_for_and_requires_the_configured_dimension(
    clients: ModelClients, server: FakeOpenRouter
) -> None:
    server.script(fixture_response("embeddings_response.json"))

    with pytest.raises(EmbeddingDimensionError) as raised:
        await clients.embedder.embed(["first text", "second text"])

    assert server.only_request.url == "https://openrouter.test/api/v1/embeddings"
    assert server.only_request.body["model"] == "openai/text-embedding-3-large"
    assert server.only_request.body["dimensions"] == 1024
    assert (raised.value.expected, raised.value.actual) == (1024, 4)
