from app.models.base import GenerationRequest, Message, OutputSchema, Video
from app.models.multimodal import MultimodalGenerator
from app.models.openrouter import OpenRouterTransport
from tests.models.conftest import Connect
from tests.models.support import (
    CHAT_COMPLETIONS_URL,
    TIMEOUT_SECONDS,
    json_response,
    load_fixture,
)

ESCALATION_MODEL = "google/gemini-2.5-flash"
ENTITY_SCHEMA = OutputSchema(name="entity", schema={"type": "object"})


def generator(transport: OpenRouterTransport) -> MultimodalGenerator:
    return MultimodalGenerator(
        transport, url=CHAT_COMPLETIONS_URL, model=ESCALATION_MODEL, timeout_seconds=TIMEOUT_SECONDS
    )


def reply_from(provider: str) -> dict[str, object]:
    body = load_fixture("chat_completion_schema.json")
    body["provider"] = provider
    body["model"] = ESCALATION_MODEL
    return body


async def test_video_travels_inline_next_to_the_text(connect: Connect) -> None:
    server, transport = connect([json_response(reply_from("Google"))])
    request = GenerationRequest(
        messages=[
            Message(
                role="user",
                content=["Which place is this?", Video(data=b"video-bytes", mime_type="video/mp4")],
            )
        ]
    )

    await generator(transport).generate(request)

    assert server.only_request.url == CHAT_COMPLETIONS_URL
    assert server.only_request.body == {
        "model": ESCALATION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Which place is this?"},
                    {
                        "type": "video_url",
                        "video_url": {"url": "data:video/mp4;base64,dmlkZW8tYnl0ZXM="},
                    },
                ],
            }
        ],
    }


async def test_escalation_model_is_not_pinned_and_accepts_any_provider(connect: Connect) -> None:
    server, transport = connect([json_response(reply_from("Google AI Studio"))])
    request = GenerationRequest(
        messages=[Message(role="user", content="a question")], output_schema=ENTITY_SCHEMA
    )

    result = await generator(transport).generate(request)

    assert server.only_request.body["provider"] == {"require_parameters": True}
    assert result.provider == "Google AI Studio"
    assert result.structured == {"category": "cafe", "entities": [{"name": "Example Roasters"}]}
