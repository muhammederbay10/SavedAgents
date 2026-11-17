"""A minimal OpenRouter client for the spike: transcription, embeddings, key usage.

Every paid request goes through the spending ledger first. The client never
logs request or response bodies, and it never sees where the API key came from.

Contracts (OpenRouter docs, 17 Nov 2026):

- `POST /audio/transcriptions`: JSON with `model`, `input_audio.data` (base64),
  `input_audio.format`, `response_format` and, for `verbose_json`,
  `timestamp_granularities`. The response holds `text` and `usage.cost`, and
  with `verbose_json` also `language`, `segments` and `words`.
- `POST /embeddings`: JSON with `model`, `input` and optionally `dimensions`.
  The response holds `data[].embedding`, `data[].index` and `usage.cost`.
- `GET /key`: `data.usage` is the key's total spend. This call is free.
"""

import base64
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

from scripts.spikes.model_choices.budget import SpendLedger, is_valid_amount
from scripts.spikes.model_choices.settings import (
    AUDIO_FORMAT,
    EMBEDDINGS_PATH,
    KEY_PATH,
    OPENROUTER_BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    TIMESTAMP_GRANULARITIES,
    TRANSCRIPTIONS_PATH,
    SpeechToTextModel,
)

JsonObject = dict[str, object]


@dataclass(frozen=True)
class ApiResult:
    """The outcome of one request, whether it worked or not.

    Attributes:
        status: HTTP status, or `None` when no response arrived.
        latency_seconds: Time from sending the request to reading the response.
        body: The response's JSON object; empty when there was none.
        returned_cost_usd: `usage.cost` from the response; `None` if it is absent
            or not a finite, non-negative number.
        error: The exception's class name when no response arrived.
    """

    status: int | None
    latency_seconds: float
    body: JsonObject = field(repr=False)
    returned_cost_usd: float | None
    error: str | None = None

    @property
    def is_success(self) -> bool:
        """Return whether the API answered with HTTP 200."""
        return self.status == httpx.codes.OK


def build_http_client(
    api_key: str, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Create the HTTP client that carries the API key.

    Args:
        api_key: The OpenRouter key, taken from the environment by the caller.
        transport: A replacement transport; tests pass a mock here.

    Returns:
        An async client bound to OpenRouter's base URL.
    """
    return httpx.AsyncClient(
        base_url=OPENROUTER_BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
        transport=transport,
    )


class OpenRouterClient:
    """Sends the spike's requests and counts every one in the spending ledger."""

    def __init__(self, http_client: httpx.AsyncClient, ledger: SpendLedger) -> None:
        """Create the client.

        Args:
            http_client: A client from `build_http_client`.
            ledger: The ledger that authorizes and records each paid request.
        """
        self._http_client = http_client
        self._ledger = ledger

    async def transcribe(
        self, model: SpeechToTextModel, audio: bytes, estimate_usd: float
    ) -> ApiResult:
        """Transcribe one WAV clip.

        Args:
            model: The model and the response format to ask for.
            audio: The WAV file's bytes.
            estimate_usd: The most this request is expected to cost.

        Returns:
            The request's outcome.

        Raises:
            SpendCapExceededError: If the request could pass the spending cap.
        """
        payload: JsonObject = {
            "model": model.model_id,
            "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": AUDIO_FORMAT,
            },
            "response_format": model.response_format,
        }
        if model.response_format == "verbose_json":
            payload["timestamp_granularities"] = list(TIMESTAMP_GRANULARITIES)
        return await self._post(TRANSCRIPTIONS_PATH, payload, estimate_usd)

    async def embed(
        self, model_id: str, texts: Sequence[str], dimensions: int | None, estimate_usd: float
    ) -> ApiResult:
        """Embed a batch of texts.

        Args:
            model_id: The exact OpenRouter model id.
            texts: The texts, embedded in one request.
            dimensions: The vector size to ask for, or `None` for the model's full size.
            estimate_usd: The most this request is expected to cost.

        Returns:
            The request's outcome.

        Raises:
            SpendCapExceededError: If the request could pass the spending cap.
        """
        payload: JsonObject = {"model": model_id, "input": list(texts)}
        if dimensions is not None:
            payload["dimensions"] = dimensions
        return await self._post(EMBEDDINGS_PATH, payload, estimate_usd)

    async def key_usage_usd(self) -> float | None:
        """Read the key's total spend, to cross-check the ledger. This call is free.

        Returns:
            The spend OpenRouter reports for the key, or `None` if it could not be
            read or is not a finite, non-negative number.
        """
        try:
            response = await self._http_client.get(KEY_PATH)
        except httpx.HTTPError:
            return None
        data = _json_object(response).get("data")
        return _amount(data.get("usage")) if isinstance(data, dict) else None

    async def _post(self, path: str, payload: JsonObject, estimate_usd: float) -> ApiResult:
        self._ledger.authorize(estimate_usd)
        started = time.perf_counter()
        try:
            response = await self._http_client.post(path, json=payload)
        except httpx.HTTPError as error:
            # A timeout can still be charged, so the estimate is counted as spent.
            self._ledger.record(None, estimate_usd)
            return ApiResult(None, time.perf_counter() - started, {}, None, type(error).__name__)
        latency_seconds = time.perf_counter() - started
        body = _json_object(response)
        usage = body.get("usage")
        returned_cost_usd = _amount(usage.get("cost")) if isinstance(usage, dict) else None
        self._ledger.record(returned_cost_usd, estimate_usd)
        return ApiResult(response.status_code, latency_seconds, body, returned_cost_usd)


def _json_object(response: httpx.Response) -> JsonObject:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _amount(value: object) -> float | None:
    # JSON can carry NaN and Infinity. They are dropped here, so they reach neither
    # the ledger nor the report.
    if isinstance(value, int | float) and is_valid_amount(value):
        return float(value)
    return None
