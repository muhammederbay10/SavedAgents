"""A `Generator` over OpenRouter's chat completions endpoint.

The text model (`deepseek.py`) and the video escalation model (`multimodal.py`)
both speak this contract (OpenRouter docs, 17 Nov 2026):

- JSON that follows a schema: `response_format` of type `json_schema`, with
  `name`, `strict` and `schema`. The reply's `content` is the JSON as a string.
- Tools: `tools` with `function` definitions, and `tool_choice`. The reply's
  `tool_calls` carry each call's `id`, its function `name`, and `arguments` as
  a JSON string. A tool's result goes back as a `tool` message with the call's id.
- Media: content parts next to the text, built by `wire.media_part`.
- Provider routing: a `provider` object. A pinned model sends the provider's
  slug in `only` and `order` with `allow_fallbacks: false`, so OpenRouter may
  use that one endpoint and nothing else.

Every request also sends `X-OpenRouter-Metadata: enabled`, so the response
carries `openrouter_metadata`: which endpoints the router considered, which one
it selected, and which it tried (`app.models.openrouter.routing`).

A pin is enforced twice. The request forbids every other provider, and the
reply's routing metadata must prove that the pinned provider, and only it,
handled the call. A reply without that proof is discarded with
`ProviderPinViolationError`. The top-level `provider` field is not part of
OpenRouter's documented chat completion response, so it is never accepted as
proof; it only counts against a reply that it contradicts.

A reply whose message is missing or has the wrong shape raises
`ModelResponseError`. It is never turned into an empty success.
"""

import json
from dataclasses import dataclass
from http import HTTPStatus

from app.models.base import (
    ContentPart,
    GenerationRequest,
    GenerationResult,
    Generator,
    Message,
    ToolCall,
    ToolDefinition,
)
from app.models.errors import (
    ModelRequestError,
    ModelResponseError,
    ModelUnavailableError,
    ProviderPinViolationError,
    ProviderRouteUnavailableError,
    SchemaOutputError,
)
from app.models.openrouter.routing import (
    ROUTING_METADATA_ENABLED,
    ROUTING_METADATA_HEADER,
    RouteEvidenceError,
    selected_provider,
    verify_pinned_route,
)
from app.models.openrouter.transport import ModelCall, OpenRouterTransport
from app.models.openrouter.wire import (
    JsonObject,
    cost_usd,
    list_or_none,
    media_part,
    object_or_none,
    string_or_none,
)

_OPERATION = "generate"
_ERROR_FINISH_REASON = "error"
_FUNCTION_TOOL_TYPE = "function"

# OpenRouter answers one of these when no endpoint satisfies the `provider`
# object. Its docs name both: 404 where they describe `only`, 503 in the error table.
_NO_ROUTE_STATUSES = frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.SERVICE_UNAVAILABLE})


@dataclass(frozen=True)
class ProviderPin:
    """The one upstream provider a model may be served by.

    Attributes:
        slug: The endpoint slug sent to OpenRouter, for example `coreweave/fp8`.
        name: The provider name OpenRouter reports in a response's `provider`
            field for that slug, for example `CoreWeave`.
    """

    slug: str
    name: str


class ChatCompletionsGenerator(Generator):
    """Generates text, schema-following JSON and tool calls with one model."""

    def __init__(
        self,
        transport: OpenRouterTransport,
        *,
        url: str,
        model: str,
        timeout_seconds: float,
        provider_pin: ProviderPin | None = None,
    ) -> None:
        """Create the client.

        Args:
            transport: The shared OpenRouter transport.
            url: The full URL of the chat completions endpoint.
            model: The model's id.
            timeout_seconds: How long one attempt may take.
            provider_pin: When set, the only provider that may serve the model.
        """
        self._transport = transport
        self._call = ModelCall(
            operation=_OPERATION,
            model=model,
            url=url,
            timeout_seconds=timeout_seconds,
            headers={ROUTING_METADATA_HEADER: ROUTING_METADATA_ENABLED},
        )
        self._provider_pin = provider_pin

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Produce the next reply of a conversation.

        Args:
            request: The messages, and optionally an output schema and tools.

        Returns:
            The model's reply.

        Raises:
            ProviderRouteUnavailableError: The model is pinned and its provider
                cannot serve the call.
            ProviderPinViolationError: The model is pinned and the reply does
                not prove that the pinned provider alone served it.
            SchemaOutputError: A schema was requested and the reply is not a
                JSON object.
            ModelResponseError: The reply's message is missing or malformed.
            ModelError: The call failed in another way.
        """
        body = await self._post(self._payload(request))
        provider = self._serving_provider(body)
        choice = self._first_choice(body)
        message = object_or_none(choice.get("message"))
        if message is None:
            raise self._response_error("the reply has no message object")
        tool_calls = self._tool_calls(message)
        text = self._text(message, has_tool_calls=bool(tool_calls))
        return GenerationResult(
            text=text,
            structured=self._structured(request, text, tool_calls),
            tool_calls=tool_calls,
            finish_reason=self._finish_reason(choice),
            model=string_or_none(body.get("model")) or self._call.model,
            provider=provider,
            cost_usd=cost_usd(body),
        )

    def _payload(self, request: GenerationRequest) -> JsonObject:
        payload: JsonObject = {
            "model": self._call.model,
            "messages": [_message(message) for message in request.messages],
        }
        if request.output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_schema.name,
                    "strict": True,
                    "schema": dict(request.output_schema.schema),
                },
            }
        if request.tools:
            payload["tools"] = [_tool(tool) for tool in request.tools]
            payload["tool_choice"] = request.tool_choice
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        provider = self._provider_preferences(request)
        if provider:
            payload["provider"] = provider
        return payload

    def _provider_preferences(self, request: GenerationRequest) -> JsonObject:
        preferences: JsonObject = {}
        if self._provider_pin is not None:
            preferences.update(
                only=[self._provider_pin.slug],
                order=[self._provider_pin.slug],
                allow_fallbacks=False,
            )
        if request.output_schema is not None or request.tools:
            # Without this an endpoint that cannot enforce a schema or call tools
            # may still be chosen, and would quietly ignore them.
            preferences["require_parameters"] = True
        return preferences

    async def _post(self, payload: JsonObject) -> JsonObject:
        try:
            return await self._transport.post(self._call, payload)
        except (ModelRequestError, ModelUnavailableError) as error:
            if self._provider_pin is None or error.status not in _NO_ROUTE_STATUSES:
                raise
            raise ProviderRouteUnavailableError(
                operation=_OPERATION,
                model=self._call.model,
                provider=self._provider_pin.slug,
                status=error.status,
            ) from error

    def _serving_provider(self, body: JsonObject) -> str | None:
        pin = self._provider_pin
        if pin is None:
            return selected_provider(body)
        try:
            return verify_pinned_route(body, provider_name=pin.name, model=self._call.model)
        except RouteEvidenceError as error:
            raise ProviderPinViolationError(
                operation=_OPERATION,
                model=self._call.model,
                expected_provider=pin.name,
                actual_provider=error.actual_provider,
                problem=error.problem,
            ) from error

    def _first_choice(self, body: JsonObject) -> JsonObject:
        choices = list_or_none(body.get("choices")) or []
        choice = object_or_none(choices[0]) if choices else None
        if choice is None:
            raise self._response_error("the response has no choices")
        if choice.get("finish_reason") == _ERROR_FINISH_REASON or choice.get("error") is not None:
            raise self._response_error("the provider reported an error in the reply")
        return choice

    def _text(self, message: JsonObject, *, has_tool_calls: bool) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        # Null content is valid only next to tool calls: the model called a tool
        # and wrote nothing. Without tool calls the reply would say nothing at all.
        if content is None and has_tool_calls:
            return ""
        raise self._response_error("the reply's message has no text and no tool calls")

    def _tool_calls(self, message: JsonObject) -> tuple[ToolCall, ...]:
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            return ()
        calls = list_or_none(raw_calls)
        if calls is None:
            raise self._response_error("the reply's tool calls are not a list")
        return tuple(self._tool_call(raw_call) for raw_call in calls)

    def _tool_call(self, raw_call: object) -> ToolCall:
        call = object_or_none(raw_call) or {}
        function = object_or_none(call.get("function")) or {}
        call_id = string_or_none(call.get("id"))
        name = string_or_none(function.get("name"))
        arguments = _tool_arguments(string_or_none(function.get("arguments")))
        if call_id is None or name is None or arguments is None:
            raise self._response_error("a tool call is missing its id, name or JSON arguments")
        if call.get("type", _FUNCTION_TOOL_TYPE) != _FUNCTION_TOOL_TYPE:
            raise self._response_error("a tool call is not a function call")
        return ToolCall(id=call_id, name=name, arguments=arguments)

    def _finish_reason(self, choice: JsonObject) -> str:
        finish_reason = choice.get("finish_reason")
        if finish_reason is None:
            return ""
        if not isinstance(finish_reason, str):
            raise self._response_error("the reply's finish reason is not text")
        return finish_reason

    def _structured(
        self, request: GenerationRequest, text: str, tool_calls: tuple[ToolCall, ...]
    ) -> JsonObject | None:
        if request.output_schema is None or tool_calls:
            return None
        structured = _json_object_from_text(text)
        if structured is None:
            raise SchemaOutputError(
                f"{_OPERATION} with {self._call.model} did not return a JSON object "
                f"for schema '{request.output_schema.name}'",
                operation=_OPERATION,
                model=self._call.model,
            )
        return structured

    def _response_error(self, problem: str) -> ModelResponseError:
        return ModelResponseError(
            f"{_OPERATION} with {self._call.model} failed: {problem}",
            operation=_OPERATION,
            model=self._call.model,
        )


def _message(message: Message) -> JsonObject:
    wire_message: JsonObject = {"role": message.role, "content": _content(message)}
    if message.tool_calls:
        wire_message["tool_calls"] = [_assistant_tool_call(call) for call in message.tool_calls]
    if message.tool_call_id is not None:
        wire_message["tool_call_id"] = message.tool_call_id
    return wire_message


def _content(message: Message) -> str | list[JsonObject] | None:
    if isinstance(message.content, str):
        # An assistant message that only calls tools has no text; the API wants null there.
        return message.content if message.content or not message.tool_calls else None
    return [_content_part(part) for part in message.content]


def _content_part(part: ContentPart) -> JsonObject:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    return media_part(part)


def _assistant_tool_call(call: ToolCall) -> JsonObject:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
    }


def _tool(tool: ToolDefinition) -> JsonObject:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
        },
    }


def _tool_arguments(text: str | None) -> JsonObject | None:
    # Some models send an empty string for a tool that takes no arguments.
    if text is not None and not text.strip():
        return {}
    return _json_object_from_text(text)


def _json_object_from_text(text: str | None) -> JsonObject | None:
    if text is None:
        return None
    try:
        return object_or_none(json.loads(text))
    except ValueError:
        return None
