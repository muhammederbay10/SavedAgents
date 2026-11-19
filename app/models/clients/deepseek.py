"""The text model client: DeepSeek V4.1 Flash, pinned to one provider.

It does extraction, memory extraction and the response agent: free text, JSON
that follows a content type's schema, and tool calls.

DeepSeek's own API is hosted in China. User data must not go there, so this
model always runs with a `ProviderPin`: the owner-approved rule (SAV-7, decision
record 0010) is one configured provider that is hosted outside China, with no
fallback to any other host. A call that cannot be served by that provider fails
with `ProviderRouteUnavailableError`. It is never sent somewhere else.
"""

from app.models.clients.chat_completions import ChatCompletionsGenerator, ProviderPin
from app.models.openrouter.transport import OpenRouterTransport


class DeepSeekGenerator(ChatCompletionsGenerator):
    """The text model. Unlike the general client, it cannot be built without a pin."""

    def __init__(
        self,
        transport: OpenRouterTransport,
        *,
        url: str,
        model: str,
        timeout_seconds: float,
        provider_pin: ProviderPin,
    ) -> None:
        """Create the client.

        Args:
            transport: The shared OpenRouter transport.
            url: The full URL of the chat completions endpoint.
            model: The text model's id.
            timeout_seconds: How long one attempt may take.
            provider_pin: The only provider that may serve the model.
        """
        super().__init__(
            transport,
            url=url,
            model=model,
            timeout_seconds=timeout_seconds,
            provider_pin=provider_pin,
        )
