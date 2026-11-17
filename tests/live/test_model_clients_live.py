"""Opt-in smoke tests: one real call per model client.

They cost a fraction of a cent, send only the invented inputs below, and are
skipped unless both `LIVE_MODEL_TESTS=1` and `OPENROUTER_API_KEY` are set in
the process environment. Like every test, they do not read `.env`; the README
("Live smoke tests") shows how to run them. A live run needs the owner's
approval first (SAV-22 §8).
"""

import io
import os
import wave
from collections.abc import AsyncIterator

import pytest

from app.config import ModelSettings
from app.models.base import (
    Audio,
    GenerationRequest,
    Message,
    OutputSchema,
    ToolDefinition,
    YesNoAnswer,
    YesNoQuestion,
)
from app.models.factory import ModelClients, build_model_clients, open_http_client

OPT_IN_VARIABLE = "LIVE_MODEL_TESTS"
API_KEY_VARIABLE = "OPENROUTER_API_KEY"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get(OPT_IN_VARIABLE) != "1" or not os.environ.get(API_KEY_VARIABLE),
        reason=f"live model tests need {OPT_IN_VARIABLE}=1 and {API_KEY_VARIABLE}",
    ),
]

INVENTED_CAPTION = "A new ramen restaurant called Example Noodle Bar opened downtown."
SILENCE_SECONDS = 1
SILENCE_SAMPLE_RATE = 16_000
SILENCE_SAMPLE_WIDTH_BYTES = 2

CATEGORY_SCHEMA = OutputSchema(
    name="category",
    schema={
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
        "additionalProperties": False,
    },
)
SEARCH_TOOL = ToolDefinition(
    name="search_items",
    description="Search the user's saved items by category.",
    parameters={
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
    },
)


@pytest.fixture
def model_settings() -> ModelSettings:
    return ModelSettings(_env_file=None)


@pytest.fixture
async def clients(model_settings: ModelSettings) -> AsyncIterator[ModelClients]:
    async with open_http_client(model_settings) as http_client:
        yield build_model_clients(model_settings, http_client)


def silent_clip() -> Audio:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(SILENCE_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SILENCE_SAMPLE_RATE)
        wav_file.writeframes(
            bytes(SILENCE_SECONDS * SILENCE_SAMPLE_RATE * SILENCE_SAMPLE_WIDTH_BYTES)
        )
    return Audio(data=buffer.getvalue(), format="wav")


async def test_decision_model_answers_a_yes_no_question(clients: ModelClients) -> None:
    question = YesNoQuestion(key="about_food", instructions="Is this post about food?")

    result = await clients.classifier.decide([INVENTED_CAPTION], [question])

    answer = result.answers["about_food"]
    assert isinstance(answer, YesNoAnswer)
    assert 0.0 <= answer.probability_yes <= 1.0


async def test_text_model_returns_schema_json_from_the_pinned_provider(
    clients: ModelClients, model_settings: ModelSettings
) -> None:
    request = GenerationRequest(
        messages=[
            Message(role="system", content="Name the post's category in one or two words."),
            Message(role="user", content=INVENTED_CAPTION),
        ],
        output_schema=CATEGORY_SCHEMA,
    )

    result = await clients.text_generator.generate(request)

    assert result.structured is not None
    assert isinstance(result.structured["category"], str)
    assert result.provider is not None
    assert result.provider.casefold() == model_settings.text_model_provider_name.casefold()


async def test_text_model_calls_a_tool(clients: ModelClients) -> None:
    request = GenerationRequest(
        messages=[Message(role="user", content="Show me the cafés I saved.")],
        tools=[SEARCH_TOOL],
        tool_choice="required",
    )

    result = await clients.text_generator.generate(request)

    assert [call.name for call in result.tool_calls] == ["search_items"]


async def test_escalation_model_replies(clients: ModelClients) -> None:
    """Text only. Sending a video is not covered here: it needs a real file."""
    request = GenerationRequest(messages=[Message(role="user", content="Reply with: ok")])

    result = await clients.escalation_generator.generate(request)

    assert result.text.strip()


async def test_transcriber_returns_a_transcript_for_silence(clients: ModelClients) -> None:
    transcript = await clients.transcriber.transcribe(silent_clip())

    assert transcript.attempts
    assert transcript.attempts[-1].outcome in {"text", "empty"}


async def test_embedder_returns_vectors_of_the_configured_size(
    clients: ModelClients, model_settings: ModelSettings
) -> None:
    result = await clients.embedder.embed(["a cosy café", "un café acogedor"])

    assert [len(vector) for vector in result.vectors] == [model_settings.embedding_dimension] * 2
