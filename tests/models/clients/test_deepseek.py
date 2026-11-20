from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.models.base import (
    GenerationRequest,
    Message,
    OutputSchema,
    ToolCall,
    ToolDefinition,
)
from app.models.clients.chat_completions import ProviderPin
from app.models.clients.deepseek import DeepSeekGenerator
from app.models.errors import (
    ModelResponseError,
    ModelUnavailableError,
    ProviderPinViolationError,
    ProviderRouteUnavailableError,
    SchemaOutputError,
)
from app.models.openrouter.transport import OpenRouterTransport
from tests.models.conftest import MAX_ATTEMPTS, Connect
from tests.models.support import (
    CHAT_COMPLETIONS_URL,
    TIMEOUT_SECONDS,
    error_response,
    fixture_response,
    json_response,
    load_fixture,
)

TEXT_MODEL = "deepseek/deepseek-v4.1-flash"
PIN = ProviderPin(slug="coreweave/fp8", name="CoreWeave")
PINNED_ROUTING = {"only": ["coreweave/fp8"], "order": ["coreweave/fp8"], "allow_fallbacks": False}

EXTRACTION_SCHEMA = OutputSchema(
    name="extraction",
    schema={
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
        "additionalProperties": False,
    },
)
SEARCH_TOOL = ToolDefinition(
    name="search_items",
    description="Search the user's saved items.",
    parameters={"type": "object", "properties": {"type": {"type": "string"}}},
)
QUESTION = GenerationRequest(messages=[Message(role="user", content="a question")])


def generator(transport: OpenRouterTransport) -> DeepSeekGenerator:
    return DeepSeekGenerator(
        transport,
        url=CHAT_COMPLETIONS_URL,
        model=TEXT_MODEL,
        timeout_seconds=TIMEOUT_SECONDS,
        provider_pin=PIN,
    )


async def test_every_request_allows_only_the_pinned_provider_and_no_fallback(
    connect: Connect,
) -> None:
    requests = [
        QUESTION,
        GenerationRequest(messages=QUESTION.messages, output_schema=EXTRACTION_SCHEMA),
        GenerationRequest(messages=QUESTION.messages, tools=[SEARCH_TOOL]),
    ]
    server, transport = connect(
        [
            fixture_response("chat_completion_schema.json"),
            fixture_response("chat_completion_schema.json"),
            fixture_response("chat_completion_tool_calls.json"),
        ]
    )

    for request in requests:
        await generator(transport).generate(request)

    for sent in server.requests:
        assert sent.url == CHAT_COMPLETIONS_URL
        assert sent.body["model"] == TEXT_MODEL
        assert sent.body["provider"]["only"] == ["coreweave/fp8"]
        assert sent.body["provider"]["order"] == ["coreweave/fp8"]
        assert sent.body["provider"]["allow_fallbacks"] is False
    assert server.requests[0].body["provider"] == PINNED_ROUTING


def routing_metadata(body: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = body["openrouter_metadata"]
    return metadata


def selected_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    endpoint: dict[str, Any] = routing_metadata(body)["endpoints"]["available"][0]
    return endpoint


def endpoint(provider: str, *, selected: bool, model: str = TEXT_MODEL) -> dict[str, object]:
    return {"provider": provider, "model": model, "selected": selected}


async def test_every_pinned_request_opts_in_to_routing_metadata(connect: Connect) -> None:
    server, transport = connect([fixture_response("chat_completion_schema.json")])

    await generator(transport).generate(QUESTION)

    assert server.only_request.routing_metadata_header == "enabled"


async def test_reply_whose_selected_endpoint_is_the_pinned_provider_is_accepted(
    connect: Connect,
) -> None:
    body = load_fixture("chat_completion_schema.json")
    assert "provider" not in body
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.provider == "CoreWeave"


async def test_selected_provider_is_compared_without_regard_to_case(connect: Connect) -> None:
    body = load_fixture("chat_completion_schema.json")
    selected_endpoint(body)["provider"] = "coreweave"
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.provider == "coreweave"


@pytest.mark.parametrize("served_by", ["DeepSeek", "Alibaba", "coreweave/fp8", ""])
async def test_reply_selected_from_another_provider_is_discarded(
    connect: Connect, served_by: str
) -> None:
    body = load_fixture("chat_completion_schema.json")
    selected_endpoint(body)["provider"] = served_by
    _, transport = connect([json_response(body)])

    with pytest.raises(ProviderPinViolationError) as raised:
        await generator(transport).generate(QUESTION)

    assert raised.value.expected_provider == "CoreWeave"
    assert raised.value.actual_provider == served_by


async def test_cache_replay_without_routing_metadata_is_discarded(connect: Connect) -> None:
    _, transport = connect([fixture_response("chat_completion_cache_replay.json")])

    with pytest.raises(ProviderPinViolationError, match="no routing metadata") as raised:
        await generator(transport).generate(QUESTION)

    assert raised.value.actual_provider is None


async def test_reply_that_went_through_another_provider_first_is_discarded(
    connect: Connect,
) -> None:
    _, transport = connect([fixture_response("chat_completion_after_fallback.json")])

    with pytest.raises(ProviderPinViolationError, match="another provider") as raised:
        await generator(transport).generate(QUESTION)

    assert raised.value.actual_provider == "DeepInfra"


def set_metadata(body: dict[str, Any], value: object) -> None:
    body["openrouter_metadata"] = value


def set_endpoints(body: dict[str, Any], value: object) -> None:
    routing_metadata(body)["endpoints"] = value


def set_available(body: dict[str, Any], value: object) -> None:
    routing_metadata(body)["endpoints"]["available"] = value


def set_attempts(body: dict[str, Any], value: object) -> None:
    routing_metadata(body)["attempts"] = value


def set_selected_field(field_name: str, value: object) -> Callable[[dict[str, Any]], None]:
    def change(body: dict[str, Any]) -> None:
        selected_endpoint(body)[field_name] = value

    return change


def attempt_record(provider: str, status: object, model: str = TEXT_MODEL) -> dict[str, object]:
    return {"provider": provider, "model": model, "status": status}


def set_attempt_number(value: object) -> Callable[[dict[str, Any]], None]:
    def change(body: dict[str, Any]) -> None:
        routing_metadata(body)["attempt"] = value

    return change


def fallback_reply(attempts: object | None) -> Callable[[dict[str, Any]], None]:
    """The Reviewer's cases: a second attempt after a fallback, with the given history."""

    def change(body: dict[str, Any]) -> None:
        metadata = routing_metadata(body)
        metadata.update(attempt=2, strategy="fallback")
        if attempts is None:
            del metadata["attempts"]
        else:
            metadata["attempts"] = attempts

    return change


def without(field_name: str) -> Callable[[dict[str, Any]], None]:
    def change(body: dict[str, Any]) -> None:
        del routing_metadata(body)[field_name]

    return change


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda body: set_metadata(body, "CoreWeave"), "no routing metadata"),
        (lambda body: set_metadata(body, None), "no routing metadata"),
        (lambda body: set_endpoints(body, None), "malformed"),
        (lambda body: set_available(body, "CoreWeave"), "malformed"),
        (lambda body: set_available(body, ["CoreWeave"]), "malformed"),
        (lambda body: set_available(body, []), "no selected endpoint"),
        (
            lambda body: set_available(body, [endpoint("CoreWeave", selected=False)]),
            "no selected endpoint",
        ),
        (set_selected_field("selected", "true"), "malformed"),
        (set_selected_field("provider", None), "malformed"),
        (set_selected_field("model", None), "malformed"),
        (
            lambda body: set_available(
                body, [endpoint("CoreWeave", selected=True), endpoint("CoreWeave", selected=True)]
            ),
            "more than one selected endpoint",
        ),
        (
            set_selected_field("model", "deepseek/deepseek-v4.1-flash-1017"),
            "another model",
        ),
        (lambda body: set_attempts(body, "CoreWeave"), "malformed"),
        (lambda body: set_attempts(body, [{"model": TEXT_MODEL, "status": 200}]), "malformed"),
        (
            fallback_reply(
                [
                    {"provider": "Novita", "model": TEXT_MODEL, "status": 503},
                    {"provider": "CoreWeave", "model": TEXT_MODEL, "status": 200},
                ]
            ),
            "another provider",
        ),
        (lambda body: body.update(provider="DeepSeek"), "contradicts"),
    ],
)
async def test_missing_malformed_ambiguous_or_contradictory_routing_evidence_fails_closed(
    connect: Connect, change: Callable[[dict[str, Any]], None], problem: str
) -> None:
    body = load_fixture("chat_completion_schema.json")
    change(body)
    _, transport = connect([json_response(body)])

    with pytest.raises(ProviderPinViolationError, match=problem):
        await generator(transport).generate(QUESTION)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (fallback_reply(None), "no complete attempt history"),
        (fallback_reply([]), "no complete attempt history"),
        (fallback_reply([attempt_record("CoreWeave", 200)]), "no complete attempt history"),
        (without("attempt"), "attempt number"),
        (set_attempt_number(0), "attempt number"),
        (
            lambda body: (set_attempt_number(0)(body), without("attempts")(body)),
            "attempt number",
        ),
        (set_attempt_number(-1), "attempt number"),
        (set_attempt_number(1.0), "attempt number"),
        (set_attempt_number(1.5), "attempt number"),
        (set_attempt_number(True), "attempt number"),
        (set_attempt_number("1"), "attempt number"),
        (set_attempt_number(None), "attempt number"),
        (
            lambda body: set_attempts(
                body, [attempt_record("CoreWeave", 502), attempt_record("CoreWeave", 200)]
            ),
            "no complete attempt history",
        ),
        (
            lambda body: (
                set_attempt_number(2)(body),
                set_attempts(
                    body, [attempt_record("CoreWeave", 200), attempt_record("CoreWeave", 502)]
                ),
            ),
            "last attempt did not succeed",
        ),
        (
            lambda body: set_attempts(body, [attempt_record("CoreWeave", "200")]),
            "malformed",
        ),
        (
            lambda body: set_attempts(
                body, [attempt_record("CoreWeave", 200, model="deepseek/deepseek-v4.1-flash-1017")]
            ),
            "another model",
        ),
    ],
)
async def test_unproven_attempt_history_fails_closed(
    connect: Connect, change: Callable[[dict[str, Any]], None], problem: str
) -> None:
    body = load_fixture("chat_completion_schema.json")
    change(body)
    _, transport = connect([json_response(body)])

    with pytest.raises(ProviderPinViolationError, match=problem):
        await generator(transport).generate(QUESTION)


async def test_repeated_attempt_on_the_pinned_provider_is_accepted(connect: Connect) -> None:
    body = load_fixture("chat_completion_schema.json")
    fallback_reply([attempt_record("CoreWeave", 502), attempt_record("CoreWeave", 200)])(body)
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.provider == "CoreWeave"


async def test_single_attempt_without_an_attempt_list_is_accepted(connect: Connect) -> None:
    body = load_fixture("chat_completion_schema.json")
    del routing_metadata(body)["attempts"]
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.provider == "CoreWeave"


async def test_other_provider_listed_only_as_an_unselected_candidate_is_accepted(
    connect: Connect,
) -> None:
    body = load_fixture("chat_completion_schema.json")
    metadata = routing_metadata(body)
    metadata["endpoints"] = {
        "total": 2,
        "available": [endpoint("CoreWeave", selected=True), endpoint("Baidu", selected=False)],
    }
    assert metadata["attempt"] == 1
    assert metadata["attempts"] == [attempt_record("CoreWeave", 200)]
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.provider == "CoreWeave"


async def test_top_level_provider_that_agrees_with_the_metadata_is_accepted(
    connect: Connect,
) -> None:
    body = load_fixture("chat_completion_schema.json")
    body["provider"] = "CoreWeave"
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.provider == "CoreWeave"


async def test_no_allowed_provider_answer_with_its_metadata_is_a_route_error(
    connect: Connect,
) -> None:
    no_route = fixture_response("error_no_allowed_providers.json")
    _, transport = connect([httpx.Response(404, json=no_route.json())])

    with pytest.raises(ProviderRouteUnavailableError) as raised:
        await generator(transport).generate(QUESTION)

    assert raised.value.status == 404


async def test_no_route_through_the_pinned_provider_is_a_clear_error_at_once(
    connect: Connect,
) -> None:
    server, transport = connect([error_response(404)])

    with pytest.raises(ProviderRouteUnavailableError) as raised:
        await generator(transport).generate(QUESTION)

    assert raised.value.provider == "coreweave/fp8"
    assert raised.value.status == 404
    assert "falling back to another provider is not allowed" in str(raised.value)
    assert len(server.requests) == 1


async def test_pinned_provider_that_stays_unavailable_is_never_replaced_by_another(
    connect: Connect,
) -> None:
    server, transport = connect([error_response(503)] * MAX_ATTEMPTS)

    with pytest.raises(ProviderRouteUnavailableError) as raised:
        await generator(transport).generate(QUESTION)

    assert isinstance(raised.value, ModelUnavailableError)
    assert raised.value.status == 503
    assert len(server.requests) == MAX_ATTEMPTS
    assert all(sent.body["provider"] == PINNED_ROUTING for sent in server.requests)


async def test_other_failures_of_a_pinned_model_keep_their_own_error_type(
    connect: Connect,
) -> None:
    _, transport = connect([error_response(502)] * MAX_ATTEMPTS)

    with pytest.raises(ModelUnavailableError) as raised:
        await generator(transport).generate(QUESTION)

    assert type(raised.value) is ModelUnavailableError


async def test_schema_request_asks_for_strict_json_schema_output(connect: Connect) -> None:
    server, transport = connect([fixture_response("chat_completion_schema.json")])
    request = GenerationRequest(
        messages=[
            Message(role="system", content="Extract the fields."),
            Message(role="user", content="a caption"),
        ],
        output_schema=EXTRACTION_SCHEMA,
        max_output_tokens=500,
        temperature=0.0,
    )

    await generator(transport).generate(request)

    assert server.only_request.body == {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": "Extract the fields."},
            {"role": "user", "content": "a caption"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "extraction",
                "strict": True,
                "schema": dict(EXTRACTION_SCHEMA.schema),
            },
        },
        "max_tokens": 500,
        "temperature": 0.0,
        "provider": {**PINNED_ROUTING, "require_parameters": True},
    }


async def test_schema_reply_is_parsed_into_a_json_object(connect: Connect) -> None:
    _, transport = connect([fixture_response("chat_completion_schema.json")])
    request = GenerationRequest(messages=QUESTION.messages, output_schema=EXTRACTION_SCHEMA)

    result = await generator(transport).generate(request)

    assert result.structured == {"category": "cafe", "entities": [{"name": "Example Roasters"}]}
    assert result.tool_calls == ()
    assert result.finish_reason == "stop"
    assert result.model == TEXT_MODEL
    assert result.provider == "CoreWeave"
    assert result.cost_usd == 0.0000559


@pytest.mark.parametrize("content", ['{"category": "caf', "Here is the JSON you asked for.", "[1]"])
async def test_schema_reply_that_is_not_a_json_object_raises_schema_error(
    connect: Connect, content: str
) -> None:
    body = load_fixture("chat_completion_schema.json")
    body["choices"][0]["message"]["content"] = content
    _, transport = connect([json_response(body)])
    request = GenerationRequest(messages=QUESTION.messages, output_schema=EXTRACTION_SCHEMA)

    with pytest.raises(SchemaOutputError, match="schema 'extraction'") as raised:
        await generator(transport).generate(request)

    assert content not in str(raised.value)


async def test_plain_reply_has_text_and_no_structured_output(connect: Connect) -> None:
    body = load_fixture("chat_completion_schema.json")
    body["choices"][0]["message"]["content"] = "Three places match."
    server, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.text == "Three places match."
    assert result.structured is None
    assert "response_format" not in server.only_request.body
    assert "tools" not in server.only_request.body


async def test_tool_request_sends_the_definitions_and_the_tool_choice(connect: Connect) -> None:
    server, transport = connect([fixture_response("chat_completion_tool_calls.json")])
    request = GenerationRequest(
        messages=QUESTION.messages, tools=[SEARCH_TOOL], tool_choice="required"
    )

    await generator(transport).generate(request)

    assert server.only_request.body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search_items",
                "description": "Search the user's saved items.",
                "parameters": dict(SEARCH_TOOL.parameters),
            },
        }
    ]
    assert server.only_request.body["tool_choice"] == "required"
    assert server.only_request.body["provider"]["require_parameters"] is True


async def test_tool_calls_come_back_with_parsed_arguments(connect: Connect) -> None:
    _, transport = connect([fixture_response("chat_completion_tool_calls.json")])
    request = GenerationRequest(messages=QUESTION.messages, tools=[SEARCH_TOOL])

    result = await generator(transport).generate(request)

    assert result.tool_calls == (
        ToolCall(
            id="call_synthetic_1",
            name="search_items",
            arguments={"type": "place", "filters": {"area": "Exampleville"}},
        ),
    )
    assert result.text == ""
    assert result.finish_reason == "tool_calls"


async def test_tool_call_with_no_arguments_text_has_empty_arguments(connect: Connect) -> None:
    body = load_fixture("chat_completion_tool_calls.json")
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = ""
    _, transport = connect([json_response(body)])
    request = GenerationRequest(messages=QUESTION.messages, tools=[SEARCH_TOOL])

    result = await generator(transport).generate(request)

    assert result.tool_calls[0].arguments == {}


@pytest.mark.parametrize(
    "broken_call",
    [
        {"type": "function", "function": {"name": "search_items", "arguments": "{}"}},
        {"id": "call_1", "type": "function", "function": {"arguments": "{}"}},
        {"id": "call_1", "type": "function", "function": {"name": "search_items"}},
        {"id": "call_1", "type": "function", "function": {"name": "s", "arguments": "{oops"}},
    ],
)
async def test_malformed_tool_call_raises_response_error(
    connect: Connect, broken_call: dict[str, object]
) -> None:
    body = load_fixture("chat_completion_tool_calls.json")
    body["choices"][0]["message"]["tool_calls"] = [broken_call]
    _, transport = connect([json_response(body)])
    request = GenerationRequest(messages=QUESTION.messages, tools=[SEARCH_TOOL])

    with pytest.raises(ModelResponseError, match="tool call is missing"):
        await generator(transport).generate(request)


async def test_tool_results_go_back_as_tool_messages_with_the_calls_id(connect: Connect) -> None:
    server, transport = connect([fixture_response("chat_completion_schema.json")])
    earlier_call = ToolCall(id="call_synthetic_1", name="search_items", arguments={"type": "place"})
    request = GenerationRequest(
        messages=[
            Message(role="user", content="a question"),
            Message(role="assistant", content="", tool_calls=[earlier_call]),
            Message(role="tool", content='{"items": []}', tool_call_id="call_synthetic_1"),
        ],
        tools=[SEARCH_TOOL],
    )

    await generator(transport).generate(request)

    assert server.only_request.body["messages"] == [
        {"role": "user", "content": "a question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_synthetic_1",
                    "type": "function",
                    "function": {"name": "search_items", "arguments": '{"type": "place"}'},
                }
            ],
        },
        {"role": "tool", "content": '{"items": []}', "tool_call_id": "call_synthetic_1"},
    ]


@pytest.mark.parametrize(
    "choices",
    [
        [],
        [{"finish_reason": "error", "error": {"code": 502, "message": "upstream"}}],
        [{"finish_reason": "stop", "error": {"code": 500, "message": "upstream"}}],
    ],
)
async def test_reply_without_a_usable_choice_raises_response_error(
    connect: Connect, choices: list[dict[str, object]]
) -> None:
    body = load_fixture("chat_completion_schema.json")
    body["choices"] = choices
    _, transport = connect([json_response(body)])

    with pytest.raises(ModelResponseError):
        await generator(transport).generate(QUESTION)


@pytest.mark.parametrize(
    "choice",
    [
        {"index": 0, "finish_reason": "stop"},
        {"index": 0, "finish_reason": "stop", "message": None},
        {"index": 0, "finish_reason": "stop", "message": "Three places match."},
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": 123}},
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": ["a"]}},
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": None}},
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant"}},
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": "search_items"},
        },
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": []},
        },
        {"index": 0, "finish_reason": 7, "message": {"role": "assistant", "content": "ok"}},
    ],
)
async def test_malformed_message_raises_response_error_instead_of_an_empty_success(
    connect: Connect, choice: dict[str, object]
) -> None:
    body = load_fixture("chat_completion_schema.json")
    # The routing evidence is valid, so only the message's shape can be at fault.
    body["provider"] = "CoreWeave"
    body["choices"] = [choice]
    _, transport = connect([json_response(body)])

    with pytest.raises(ModelResponseError) as raised:
        await generator(transport).generate(QUESTION)

    assert type(raised.value) is ModelResponseError


async def test_tool_call_of_another_type_raises_response_error(connect: Connect) -> None:
    body = load_fixture("chat_completion_tool_calls.json")
    body["provider"] = "CoreWeave"
    body["choices"][0]["message"]["tool_calls"][0]["type"] = "code_interpreter"
    _, transport = connect([json_response(body)])
    request = GenerationRequest(messages=QUESTION.messages, tools=[SEARCH_TOOL])

    with pytest.raises(ModelResponseError) as raised:
        await generator(transport).generate(request)

    assert type(raised.value) is ModelResponseError


async def test_tool_only_reply_with_null_content_is_valid(connect: Connect) -> None:
    body = load_fixture("chat_completion_tool_calls.json")
    assert body["choices"][0]["message"]["content"] is None
    _, transport = connect([json_response(body)])
    request = GenerationRequest(messages=QUESTION.messages, tools=[SEARCH_TOOL])

    result = await generator(transport).generate(request)

    assert result.text == ""
    assert [call.name for call in result.tool_calls] == ["search_items"]


async def test_empty_text_reply_is_a_valid_shape(connect: Connect) -> None:
    body = load_fixture("chat_completion_schema.json")
    body["choices"][0]["message"]["content"] = ""
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.text == ""
    assert result.tool_calls == ()


async def test_text_reply_with_an_empty_tool_call_list_is_valid(connect: Connect) -> None:
    body = load_fixture("chat_completion_schema.json")
    body["choices"][0]["message"].update(content="Three places match.", tool_calls=[])
    _, transport = connect([json_response(body)])

    result = await generator(transport).generate(QUESTION)

    assert result.text == "Three places match."
    assert result.tool_calls == ()
