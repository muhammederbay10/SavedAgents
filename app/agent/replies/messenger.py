"""Sends the bot's DM replies through the Instagram Send API.

`docs/ARCHITECTURE.md` §3.1 and §3.5, decision record 0004. One message is one
`POST {graph api}/{ig-user-id}/messages`:

    {"recipient": {"id": "<IGSID>"}, "message": {"text": "…"}}

A reply that quotes a saved reel adds `"reply_to": {"mid": "<the save's mid>"}`
next to `message`, and a "which one?" question adds `message.quick_replies`.

Two rules decide whether a message is sent at all:

- **The reply window.** Instagram only accepts a message within 24 hours of
  the user's last message. The caller passes that time (Meta's timestamp of the
  user's last message), and a message for an older conversation is skipped and
  logged instead of being sent. Meta's own "outside the window" rejection is
  reported the same way.
- **The window is checked before every request**, also before a retry, so a
  wait between attempts can't carry a message past it.
- **Only failures that show the message was not taken are retried**, with
  exponential backoff: Meta's throttling errors (the call was refused) and
  connections that never opened (nothing was sent). Any other failure after the
  request went out, such as a timeout or any 5xx including 503, may have
  delivered the message, so it is raised as `MessengerDeliveryUnknownError` and
  not repeated. This keeps our retries from causing duplicates; it can't rule
  out one if a throttling answer was itself wrong.

Nothing personal is logged: no recipient id, message id, text or Meta error
message. Logs hold the kind of message, the attempt, the HTTP status and
Meta's numeric error code.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
from typing import Final

import httpx

from app.config.messenger import MAX_INSTAGRAM_REPLY_WINDOW_HOURS, MessengerSettings

logger = logging.getLogger(__name__)

# Limits Meta documents for Instagram messages (Instagram API, "Messaging API"
# and "Quick replies" pages). They are properties of the platform, not settings.
MAX_TEXT_BYTES: Final = 1000
MAX_QUICK_REPLIES: Final = 13

# Meta's error codes for throttling (Graph API "Rate limiting" page): app,
# user, page and custom limits, and the Instagram and Messenger business limits.
_RATE_LIMIT_ERROR_CODES: Final = frozenset({4, 17, 32, 613, 80002, 80006})

# Code 10 with one of these subcodes: "This message is sent outside of allowed
# window" (Messenger Platform "Error codes" page).
_PERMISSION_ERROR_CODE: Final = 10
_OUTSIDE_WINDOW_SUBCODES: Final = frozenset({2018278, 2534022})

# The connection never opened, so the request certainly was not sent.
_NOT_SENT_TRANSPORT_ERRORS: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

_BACKOFF_FACTOR: Final = 2
_ELAPSED_SECONDS_DECIMALS: Final = 3
_HOURS_DECIMALS: Final = 1

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]
JsonObject = dict[str, object]


class MessageKind(StrEnum):
    """What a message is, for logs."""

    TEXT = "text"
    QUICK_REPLIES = "quick_replies"
    REPLY = "reply"


class SendStatus(StrEnum):
    """What happened to a message that raised no error."""

    SENT = "sent"
    OUTSIDE_REPLY_WINDOW = "outside_reply_window"


@dataclass(frozen=True)
class QuickReply:
    """A button under a message. Tapping it sends `payload` back in a webhook.

    Attributes:
        title: The button's label. Meta cuts it after 20 characters.
        payload: What the webhook reports when the user taps the button.
    """

    title: str
    payload: str

    def __post_init__(self) -> None:
        """Reject an empty title or payload.

        Raises:
            ValueError: If either is empty or only whitespace.
        """
        if not self.title.strip():
            raise ValueError("a quick reply needs a title")
        if not self.payload.strip():
            raise ValueError("a quick reply needs a payload")


@dataclass(frozen=True)
class OutgoingMessage:
    """One DM the bot sends.

    Attributes:
        text: The message text, at most `MAX_TEXT_BYTES` bytes in UTF-8.
        quick_replies: Buttons under the message, at most `MAX_QUICK_REPLIES`.
        reply_to_message_id: The `mid` of the message this one replies to.
            Instagram then shows that message, e.g. the saved reel, above the
            text (decision record 0004).
    """

    text: str
    quick_replies: tuple[QuickReply, ...] = ()
    reply_to_message_id: str | None = None

    def __post_init__(self) -> None:
        """Reject a message Meta would refuse.

        Raises:
            ValueError: If the text is empty or too long, there are too many
                quick replies, or the reply-to id is empty.
        """
        if not self.text.strip():
            raise ValueError("a message needs text")
        if len(self.text.encode()) > MAX_TEXT_BYTES:
            raise ValueError(f"a message's text must be at most {MAX_TEXT_BYTES} bytes")
        if len(self.quick_replies) > MAX_QUICK_REPLIES:
            raise ValueError(f"a message can have at most {MAX_QUICK_REPLIES} quick replies")
        if self.reply_to_message_id is not None and not self.reply_to_message_id.strip():
            raise ValueError("reply_to_message_id must not be empty; use None for no reply")

    @property
    def kind(self) -> MessageKind:
        """The kind of message, for logs."""
        if self.reply_to_message_id is not None:
            return MessageKind.REPLY
        if self.quick_replies:
            return MessageKind.QUICK_REPLIES
        return MessageKind.TEXT


@dataclass(frozen=True)
class SendResult:
    """The outcome of a message that raised no error.

    Attributes:
        status: Whether it was sent or skipped because the window had closed.
        message_id: Meta's id of the sent message; `None` when it was skipped.
        attempts: How many requests were made; 0 when it was skipped before any.
    """

    status: SendStatus
    message_id: str | None
    attempts: int


class MessengerError(Exception):
    """A message could not be sent.

    The message never contains the text, the recipient or Meta's error message.

    Attributes:
        status: The last HTTP status, or `None` when no response arrived.
        error_code: Meta's numeric error code, when the response had one.
        error_subcode: Meta's numeric error subcode, when the response had one.
        attempts: How many requests were made.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None,
        error_code: int | None,
        error_subcode: int | None,
        attempts: int,
    ) -> None:
        """Create the error.

        Args:
            message: What went wrong, without personal data.
            status: The last HTTP status, if any.
            error_code: Meta's error code, if any.
            error_subcode: Meta's error subcode, if any.
            attempts: How many requests were made.
        """
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.error_subcode = error_subcode
        self.attempts = attempts


class MessengerRateLimitError(MessengerError):
    """Meta was still throttling on the last attempt. The message was not sent."""


class MessengerUnavailableError(MessengerError):
    """The connection to Meta never opened, on every attempt. Nothing was sent."""


class MessengerDeliveryUnknownError(MessengerError):
    """The request went out but no clear answer came back. It may have been sent.

    A timeout after sending or any 5xx, including 503. It is not retried, so a
    retry can't give the user the same message twice.
    """


class MessengerRequestError(MessengerError):
    """Meta refused the message, e.g. a bad token, a blocked user or a bad body."""


class MessengerResponseError(MessengerError):
    """Meta answered 200 without a usable message id."""


@dataclass(frozen=True)
class _Failure:
    error_class: type[MessengerError]
    reason: str
    status: int | None = None
    error_code: int | None = None
    error_subcode: int | None = None
    retry_after_seconds: float | None = None

    @property
    def is_retryable(self) -> bool:
        return self.error_class in (MessengerRateLimitError, MessengerUnavailableError)

    @property
    def is_outside_reply_window(self) -> bool:
        return (
            self.error_code == _PERMISSION_ERROR_CODE
            and self.error_subcode in _OUTSIDE_WINDOW_SUBCODES
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_message_body(recipient_id: str, message: OutgoingMessage) -> JsonObject:
    """Build the JSON body of one Send API request.

    Args:
        recipient_id: The user's Instagram-scoped id (IGSID).
        message: The message to send.

    Returns:
        The body, e.g. `{"recipient": {"id": …}, "message": {"text": …},
        "reply_to": {"mid": …}}` for a reply.
    """
    message_body: JsonObject = {"text": message.text}
    if message.quick_replies:
        message_body["quick_replies"] = [
            {"content_type": "text", "title": reply.title, "payload": reply.payload}
            for reply in message.quick_replies
        ]
    body: JsonObject = {"recipient": {"id": recipient_id}, "message": message_body}
    if message.reply_to_message_id is not None:
        body["reply_to"] = {"mid": message.reply_to_message_id}
    return body


class InstagramMessenger:
    """Sends DMs from the bot's Instagram account."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: MessengerSettings,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = _utc_now,
    ) -> None:
        """Create the messenger.

        Args:
            http_client: The HTTP client to send with. The caller owns and
                closes it; tests pass one with a mock transport.
            settings: The Send API settings.
            sleep: Waits between attempts; tests pass a recorder here.
            clock: Returns the current time in UTC; tests pass a fixed one.
        """
        self._http_client = http_client
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        base_url = str(settings.instagram_graph_api_url).rstrip("/")
        self._messages_url = f"{base_url}/{settings.instagram_account_id}/messages"
        # Settings validation already caps the window; this keeps settings built
        # without validation from widening it.
        self._reply_window_hours = min(
            settings.instagram_reply_window_hours, MAX_INSTAGRAM_REPLY_WINDOW_HOURS
        )

    async def send(
        self, recipient_id: str, message: OutgoingMessage, last_user_message_at: datetime
    ) -> SendResult:
        """Send one message, unless the reply window has closed.

        Args:
            recipient_id: The user's Instagram-scoped id (IGSID).
            message: The message to send.
            last_user_message_at: When the user last wrote to the bot, as Meta
                reported it (`received_at` of their latest `ChatMessage` or
                `CaptureEvent`). It must carry a time zone.

        Returns:
            `SENT` with Meta's message id, or `OUTSIDE_REPLY_WINDOW` when the
            message was skipped because the window had closed, by our clock or
            by Meta's answer.

        Raises:
            ValueError: If `recipient_id` is empty or `last_user_message_at`
                has no time zone.
            MessengerRateLimitError: Still throttled after the last attempt.
            MessengerUnavailableError: The connection never opened, on every attempt.
            MessengerDeliveryUnknownError: The request went out, but the answer
                was a timeout or a server error (5xx). Not retried.
            MessengerRequestError: Meta refused the message.
            MessengerResponseError: Meta answered without a message id.
        """
        if not recipient_id.strip():
            raise ValueError("recipient_id must not be empty")
        if last_user_message_at.tzinfo is None or last_user_message_at.utcoffset() is None:
            raise ValueError("last_user_message_at must carry a time zone")
        body = build_message_body(recipient_id, message)
        return await self._send_with_retries(body, message, last_user_message_at)

    async def _send_with_retries(
        self, body: JsonObject, message: OutgoingMessage, last_user_message_at: datetime
    ) -> SendResult:
        started = time.perf_counter()
        max_attempts = self._settings.send_api_max_attempts
        for attempt in range(1, max_attempts + 1):
            hours_since = (self._clock() - last_user_message_at) / timedelta(hours=1)
            if hours_since > self._reply_window_hours:
                requests_made = attempt - 1
                _log_outside_window(
                    message.kind, "reply_window_elapsed", hours_since, attempts=requests_made
                )
                return SendResult(SendStatus.OUTSIDE_REPLY_WINDOW, None, attempts=requests_made)
            outcome = await self._attempt(body)
            if isinstance(outcome, str):
                _log_attempt(logging.INFO, "dm_sent", message.kind, attempt, started)
                return SendResult(SendStatus.SENT, message_id=outcome, attempts=attempt)
            if outcome.is_outside_reply_window:
                _log_outside_window(message.kind, "meta_rejected", None, attempts=attempt)
                return SendResult(SendStatus.OUTSIDE_REPLY_WINDOW, None, attempts=attempt)
            if not outcome.is_retryable or attempt == max_attempts:
                _log_attempt(
                    logging.WARNING, "dm_send_failed", message.kind, attempt, started, outcome
                )
                raise _error_for(outcome, attempt)
            delay_seconds = self._delay_before_next_attempt(attempt, outcome)
            _log_attempt(
                logging.INFO,
                "dm_send_retrying",
                message.kind,
                attempt,
                started,
                outcome,
                delay_seconds,
            )
            await self._sleep(delay_seconds)
        raise AssertionError("unreachable: the last attempt either returned or raised")

    async def _attempt(self, body: JsonObject) -> str | _Failure:
        token = self._settings.instagram_access_token.get_secret_value()
        try:
            response = await self._http_client.post(
                self._messages_url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._settings.send_api_timeout_seconds,
            )
        except _NOT_SENT_TRANSPORT_ERRORS as error:
            return _Failure(MessengerUnavailableError, f"no connection ({type(error).__name__})")
        except httpx.TransportError as error:
            return _Failure(
                MessengerDeliveryUnknownError, f"no answer after sending ({type(error).__name__})"
            )
        return _read_response(response)

    def _delay_before_next_attempt(self, attempt: int, failure: _Failure) -> float:
        backoff = self._settings.send_api_retry_base_delay_seconds * _BACKOFF_FACTOR ** (
            attempt - 1
        )
        requested = failure.retry_after_seconds or 0.0
        delay_seconds: float = min(
            max(backoff, requested), self._settings.send_api_retry_max_delay_seconds
        )
        return delay_seconds


def _read_response(response: httpx.Response) -> str | _Failure:
    body = _json_object(response)
    error = _object_or_none(body.get("error")) if body is not None else None
    if response.status_code == HTTPStatus.OK and error is None:
        message_id = body.get("message_id") if body is not None else None
        if isinstance(message_id, str) and message_id:
            return message_id
        return _Failure(
            MessengerResponseError, "the answer had no message id", status=response.status_code
        )
    error_code = _int_or_none(error.get("code")) if error is not None else None
    return _Failure(
        _failure_class(response.status_code, error_code),
        f"HTTP {response.status_code}",
        status=response.status_code,
        error_code=error_code,
        error_subcode=_int_or_none(error.get("error_subcode")) if error is not None else None,
        retry_after_seconds=_retry_after_seconds(response),
    )


def _failure_class(status: int, error_code: int | None) -> type[MessengerError]:
    if status == HTTPStatus.TOO_MANY_REQUESTS or error_code in _RATE_LIMIT_ERROR_CODES:
        return MessengerRateLimitError
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return MessengerDeliveryUnknownError
    return MessengerRequestError


def _json_object(response: httpx.Response) -> JsonObject | None:
    try:
        return _object_or_none(response.json())
    except ValueError:
        return None


def _object_or_none(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _int_or_none(value: object) -> int | None:
    # `bool` is an `int` in Python, but never a Meta error code.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _retry_after_seconds(response: httpx.Response) -> float | None:
    # Only the seconds form is read. The HTTP-date form falls back to the backoff.
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _error_for(failure: _Failure, attempts: int) -> MessengerError:
    return failure.error_class(
        f"sending a DM failed after {attempts} attempt(s): {failure.reason}",
        status=failure.status,
        error_code=failure.error_code,
        error_subcode=failure.error_subcode,
        attempts=attempts,
    )


def _log_outside_window(
    kind: MessageKind, reason: str, hours_since: float | None, attempts: int
) -> None:
    fields: dict[str, object] = {"kind": kind.value, "reason": reason, "attempts": attempts}
    if hours_since is not None:
        fields["hours_since_last_user_message"] = round(hours_since, _HOURS_DECIMALS)
    logger.info("dm_skipped_outside_reply_window", extra=fields)


def _log_attempt(
    level: int,
    event: str,
    kind: MessageKind,
    attempt: int,
    started: float,
    failure: _Failure | None = None,
    delay_seconds: float | None = None,
) -> None:
    fields: dict[str, object] = {
        "kind": kind.value,
        "attempt": attempt,
        "elapsed_seconds": round(time.perf_counter() - started, _ELAPSED_SECONDS_DECIMALS),
    }
    if failure is not None:
        fields.update(
            status=failure.status,
            error_code=failure.error_code,
            error_subcode=failure.error_subcode,
            reason=failure.reason,
        )
    if delay_seconds is not None:
        fields["delay_seconds"] = delay_seconds
    logger.log(level, event, extra=fields)
