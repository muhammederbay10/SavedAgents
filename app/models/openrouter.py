"""The HTTP transport every OpenRouter client shares.

It adds the API key, applies each call's timeout, retries transient failures
with exponential backoff, and turns every failure into one of the error types
in `app.models.errors`.

It never logs a request or response body, and its errors never quote one: the
bodies hold users' captions, transcripts, messages and media.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus

import httpx

from app.models.errors import (
    ModelAuthenticationError,
    ModelCreditsError,
    ModelError,
    ModelRateLimitError,
    ModelRequestError,
    ModelResponseError,
    ModelUnavailableError,
)
from app.models.wire import JsonObject, number_or_none, object_or_none, string_or_none

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"
TRANSCRIPTIONS_PATH = "/audio/transcriptions"
EMBEDDINGS_PATH = "/embeddings"

# Cloudflare's "a timeout occurred" and the "provider is overloaded" status that
# OpenRouter passes on. Neither has a name in `http.HTTPStatus`.
_ORIGIN_TIMEOUT_STATUS = 524
_PROVIDER_OVERLOADED_STATUS = 529

# A later attempt can succeed for these. Everything else fails at once.
_TRANSIENT_STATUSES = frozenset(
    {
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
        _ORIGIN_TIMEOUT_STATUS,
        _PROVIDER_OVERLOADED_STATUS,
    }
)

_BACKOFF_FACTOR = 2
_ELAPSED_SECONDS_DECIMALS = 3

# Error labels are short identifiers such as `rate_limit_exceeded`. Anything
# else in that field is dropped, so free text can never reach a log line.
_ERROR_TYPE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    """How often a failed call is repeated, and how long to wait in between.

    Attributes:
        max_attempts: Total attempts, including the first.
        base_delay_seconds: The wait before the second attempt. It doubles
            before each later one.
        max_delay_seconds: The longest wait before any attempt.
    """

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float


@dataclass(frozen=True)
class ModelCall:
    """What one request is for. Used for the request itself, logs and errors.

    Attributes:
        operation: The interface method: `decide`, `generate`, `transcribe` or `embed`.
        model: The configured id of the model.
        url: The full URL of the endpoint.
        timeout_seconds: How long one attempt may take.
        headers: Extra request headers for this call, such as the opt-in to
            routing metadata.
    """

    operation: str
    model: str
    url: str
    timeout_seconds: float
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Failure:
    status: int | None
    error_type: str | None = None
    cause: str | None = None
    retry_after_seconds: float | None = None
    is_unreadable_response: bool = False

    @property
    def is_transient(self) -> bool:
        if self.is_unreadable_response:
            return False
        return self.status is None or self.status in _TRANSIENT_STATUSES


def build_http_client(
    api_key: str, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Create the HTTP client that carries the OpenRouter API key.

    The caller owns the client and closes it with `aclose()`.

    Args:
        api_key: The OpenRouter key.
        transport: A replacement transport; tests pass a mock here.

    Returns:
        An async client that sends the key on every request.
    """
    return httpx.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, transport=transport)


def endpoint_url(base_url: str, path: str) -> str:
    """Join the API's base URL and an endpoint path.

    Args:
        base_url: For example `https://openrouter.ai/api/v1`.
        path: For example `/embeddings`.

    Returns:
        The endpoint's full URL.
    """
    return f"{base_url.rstrip('/')}{path}"


class OpenRouterTransport:
    """Sends JSON requests to OpenRouter and returns the JSON answer."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        retry_policy: RetryPolicy,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Create the transport.

        Args:
            http_client: A client from `build_http_client`.
            retry_policy: How transient failures are retried.
            sleep: Waits between attempts; tests pass a recorder here.
        """
        self._http_client = http_client
        self._retry_policy = retry_policy
        self._sleep = sleep

    async def post(self, call: ModelCall, payload: JsonObject) -> JsonObject:
        """Send one request, retrying transient failures.

        Args:
            call: What the request is for, and where it goes.
            payload: The JSON body.

        Returns:
            The response's JSON object. It is a successful answer: error
            statuses and error bodies have already been raised.

        Raises:
            ModelAuthenticationError: The key was refused.
            ModelCreditsError: The account has no credits left.
            ModelRequestError: The request was rejected for another reason.
            ModelRateLimitError: Still rate-limited after the last attempt.
            ModelUnavailableError: Timeouts or server errors on every attempt.
            ModelResponseError: The answer was not a JSON object.
        """
        started = time.perf_counter()
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            outcome = await self._attempt(call, payload)
            if not isinstance(outcome, _Failure):
                _log_call(logging.INFO, "model_call_succeeded", call, attempt, started)
                return outcome
            if not outcome.is_transient or attempt == self._retry_policy.max_attempts:
                _log_call(logging.WARNING, "model_call_failed", call, attempt, started, outcome)
                raise _error_for(call, outcome)
            delay_seconds = self._delay_before_next_attempt(attempt, outcome)
            _log_call(logging.INFO, "model_call_retrying", call, attempt, started, outcome)
            await self._sleep(delay_seconds)
        raise AssertionError("unreachable: the last attempt either returned or raised")

    async def _attempt(self, call: ModelCall, payload: JsonObject) -> JsonObject | _Failure:
        try:
            response = await self._http_client.post(
                call.url, json=payload, headers=dict(call.headers), timeout=call.timeout_seconds
            )
        except httpx.TransportError as error:
            return _Failure(status=None, cause=type(error).__name__)
        return _read_response(response)

    def _delay_before_next_attempt(self, attempt: int, failure: _Failure) -> float:
        backoff = self._retry_policy.base_delay_seconds * _BACKOFF_FACTOR ** (attempt - 1)
        requested = failure.retry_after_seconds or 0.0
        delay_seconds: float = min(max(backoff, requested), self._retry_policy.max_delay_seconds)
        return delay_seconds


def _read_response(response: httpx.Response) -> JsonObject | _Failure:
    body = _json_object(response)
    error = object_or_none(body.get("error")) if body is not None else None
    if response.status_code == HTTPStatus.OK and error is None:
        if body is None:
            return _Failure(status=response.status_code, is_unreadable_response=True)
        return body
    # OpenRouter can answer HTTP 200 with only an `error` object in the body.
    # Its `code` then says what the status would have been.
    status = _error_code(error) if response.status_code == HTTPStatus.OK else response.status_code
    return _Failure(
        status=status,
        error_type=_error_type(error),
        retry_after_seconds=_retry_after_seconds(response),
        is_unreadable_response=status is None,
    )


def _json_object(response: httpx.Response) -> JsonObject | None:
    try:
        return object_or_none(response.json())
    except ValueError:
        return None


def _error_code(error: JsonObject | None) -> int | None:
    code = number_or_none(error.get("code")) if error is not None else None
    return int(code) if code is not None else None


def _error_type(error: JsonObject | None) -> str | None:
    metadata = object_or_none(error.get("metadata")) if error is not None else None
    label = string_or_none(metadata.get("error_type")) if metadata is not None else None
    if label is None or not _ERROR_TYPE_PATTERN.fullmatch(label):
        return None
    return label


def _retry_after_seconds(response: httpx.Response) -> float | None:
    # Only the seconds form is read. The HTTP-date form falls back to the backoff.
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _error_for(call: ModelCall, failure: _Failure) -> ModelError:
    error_class = _error_class(failure)
    return error_class(
        _error_message(call, failure),
        operation=call.operation,
        model=call.model,
        status=failure.status,
        error_type=failure.error_type,
    )


def _error_class(failure: _Failure) -> type[ModelError]:
    if failure.is_unreadable_response:
        return ModelResponseError
    if failure.status == HTTPStatus.UNAUTHORIZED:
        return ModelAuthenticationError
    if failure.status == HTTPStatus.PAYMENT_REQUIRED:
        return ModelCreditsError
    if failure.status == HTTPStatus.TOO_MANY_REQUESTS:
        return ModelRateLimitError
    if failure.is_transient:
        return ModelUnavailableError
    return ModelRequestError


def _error_message(call: ModelCall, failure: _Failure) -> str:
    prefix = f"{call.operation} with {call.model} failed"
    if failure.is_unreadable_response:
        return f"{prefix}: the response was not a usable JSON object"
    if failure.status is None:
        return f"{prefix}: no response ({failure.cause})"
    label = f", {failure.error_type}" if failure.error_type else ""
    return f"{prefix}: HTTP {failure.status}{label}"


def _log_call(
    level: int,
    event: str,
    call: ModelCall,
    attempt: int,
    started: float,
    failure: _Failure | None = None,
) -> None:
    fields: dict[str, object] = {
        "operation": call.operation,
        "model": call.model,
        "attempt": attempt,
        "elapsed_seconds": round(time.perf_counter() - started, _ELAPSED_SECONDS_DECIMALS),
    }
    if failure is not None:
        fields.update(status=failure.status, error_type=failure.error_type, cause=failure.cause)
    logger.log(level, event, extra=fields)
