"""The escalation model client: a generative model that watches the video itself.

It is used only when the caption and the transcript gave no entity, for example
a silent reel whose only information is a text overlay (ARCHITECTURE §3.3). The
video travels inline in a message, as a `Video` content part next to the text:

    Message(role="user", content=["Which place is this?", Video(data, "video/mp4")])

The model is not pinned to a provider.
"""

from app.models.clients.chat_completions import ChatCompletionsGenerator
from app.models.openrouter.transport import OpenRouterTransport


class MultimodalGenerator(ChatCompletionsGenerator):
    """The video escalation model."""

    def __init__(
        self,
        transport: OpenRouterTransport,
        *,
        url: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        """Create the client.

        Args:
            transport: The shared OpenRouter transport.
            url: The full URL of the chat completions endpoint.
            model: The escalation model's id.
            timeout_seconds: How long one attempt may take. Videos take longer
                than text, so this is its own setting.
        """
        super().__init__(transport, url=url, model=model, timeout_seconds=timeout_seconds)
