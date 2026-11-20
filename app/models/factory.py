"""Builds every model client from the settings.

This is the one place that decides which client serves which role. Code that
needs a model takes the interface (`Classifier`, `Generator`, …) and is handed
a client from here, or a fake in tests.

    settings = get_model_settings()
    async with open_http_client(settings) as http_client:
        models = build_model_clients(settings, http_client)
        result = await models.embedder.embed(["a cosy café"])
"""

from dataclasses import dataclass

import httpx

from app.config.models import ModelSettings
from app.models.base import Classifier, Embedder, Generator, Transcriber
from app.models.clients.chat_completions import ProviderPin
from app.models.clients.decision import OpenRouterClassifier
from app.models.clients.deepseek import DeepSeekGenerator
from app.models.clients.embeddings import OpenRouterEmbedder
from app.models.clients.multimodal import MultimodalGenerator
from app.models.clients.transcriber import FallbackTranscriber, OpenRouterTranscriber
from app.models.openrouter.transport import (
    CHAT_COMPLETIONS_PATH,
    EMBEDDINGS_PATH,
    TRANSCRIPTIONS_PATH,
    OpenRouterTransport,
    RetryPolicy,
    build_http_client,
    endpoint_url,
)


@dataclass(frozen=True)
class ModelClients:
    """One client per model role (ARCHITECTURE §3.9).

    Attributes:
        classifier: Fixed decisions with probabilities.
        transcriber: Speech-to-text: the default model, then the fallback.
        text_generator: Extraction and the response agent, on the pinned provider.
        escalation_generator: The video model for the rare escalation.
        embedder: Embeddings of the configured size.
    """

    classifier: Classifier
    transcriber: Transcriber
    text_generator: Generator
    escalation_generator: Generator
    embedder: Embedder


def open_http_client(
    settings: ModelSettings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Create the HTTP client the model clients share.

    Use it as an async context manager, or close it with `aclose()`.

    Args:
        settings: The model settings; only the API key is read.
        transport: A replacement transport; tests pass a mock here.

    Returns:
        A client that sends the OpenRouter key on every request.
    """
    return build_http_client(settings.openrouter_api_key.get_secret_value(), transport)


def build_model_clients(settings: ModelSettings, http_client: httpx.AsyncClient) -> ModelClients:
    """Create the client for every model role.

    Args:
        settings: Model ids, endpoints, timeouts, retries and the provider pin.
        http_client: A client from `open_http_client`. The caller closes it.

    Returns:
        The clients, each behind its provider interface.
    """
    transport = OpenRouterTransport(
        http_client,
        RetryPolicy(
            max_attempts=settings.openrouter_max_attempts,
            base_delay_seconds=settings.openrouter_retry_base_delay_seconds,
            max_delay_seconds=settings.openrouter_retry_max_delay_seconds,
        ),
    )
    base_url = str(settings.openrouter_base_url)
    chat_completions_url = endpoint_url(base_url, CHAT_COMPLETIONS_PATH)
    return ModelClients(
        classifier=OpenRouterClassifier(
            transport,
            url=str(settings.openrouter_decisions_url),
            model=settings.decision_model,
            timeout_seconds=settings.decision_timeout_seconds,
        ),
        transcriber=_transcriber(settings, transport, endpoint_url(base_url, TRANSCRIPTIONS_PATH)),
        text_generator=DeepSeekGenerator(
            transport,
            url=chat_completions_url,
            model=settings.text_model,
            timeout_seconds=settings.text_timeout_seconds,
            provider_pin=ProviderPin(
                slug=settings.text_model_provider, name=settings.text_model_provider_name
            ),
        ),
        escalation_generator=MultimodalGenerator(
            transport,
            url=chat_completions_url,
            model=settings.escalation_model,
            timeout_seconds=settings.escalation_timeout_seconds,
        ),
        embedder=OpenRouterEmbedder(
            transport,
            url=endpoint_url(base_url, EMBEDDINGS_PATH),
            model=settings.embedding_model,
            dimensions=settings.embedding_dimension,
            timeout_seconds=settings.embedding_timeout_seconds,
        ),
    )


def _transcriber(settings: ModelSettings, transport: OpenRouterTransport, url: str) -> Transcriber:
    return FallbackTranscriber(
        primary=OpenRouterTranscriber(
            transport,
            url=url,
            model=settings.transcription_model,
            response_format=settings.transcription_response_format,
            timeout_seconds=settings.transcription_timeout_seconds,
        ),
        fallback=OpenRouterTranscriber(
            transport,
            url=url,
            model=settings.transcription_fallback_model,
            response_format=settings.transcription_fallback_response_format,
            timeout_seconds=settings.transcription_timeout_seconds,
        ),
    )
