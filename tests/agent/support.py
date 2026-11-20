"""Helpers for the messenger tests: settings, a recording Send API and a fake clock."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import HttpUrl, SecretStr

from app.agent.replies.messenger import InstagramMessenger
from app.config.messenger import MessengerSettings

FIXTURES = Path(__file__).parent.parent / "fixtures" / "send_api"

# Synthetic values. The real ones only ever live in `.env`.
GRAPH_API_URL = "https://graph.example.test/v27.0"
BOT_ACCOUNT_ID = "17840000000000001"
ACCESS_TOKEN = "synthetic-access-token-3f9a"
MESSAGES_URL = f"{GRAPH_API_URL}/{BOT_ACCOUNT_ID}/messages"
RECIPIENT_ID = "1000000000000001"
SAVED_MESSAGE_ID = "synthetic-saved-reel-mid-77"

MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 4.0
REPLY_WINDOW_HOURS = 24.0

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def messenger_settings(max_attempts: int = MAX_ATTEMPTS) -> MessengerSettings:
    return MessengerSettings(
        _env_file=None,
        instagram_graph_api_url=HttpUrl(GRAPH_API_URL),
        instagram_account_id=BOT_ACCOUNT_ID,
        instagram_access_token=SecretStr(ACCESS_TOKEN),
        instagram_reply_window_hours=REPLY_WINDOW_HOURS,
        send_api_timeout_seconds=10.0,
        send_api_max_attempts=max_attempts,
        send_api_retry_base_delay_seconds=BASE_DELAY_SECONDS,
        send_api_retry_max_delay_seconds=MAX_DELAY_SECONDS,
    )


def fixture_body(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def json_response(
    name: str, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=fixture_body(name), headers=headers)


def sent() -> httpx.Response:
    return json_response("synthetic_send_success.json")


def rate_limited(status: int = 400, headers: dict[str, str] | None = None) -> httpx.Response:
    return json_response("synthetic_error_rate_limited.json", status, headers)


Answer = httpx.Response | Exception


@dataclass
class RecordingSendApi:
    """A mock Send API that answers from a script and records every request.

    A recorded request counts as delivered, also when its scripted answer is an
    error: that is how a server that took the message and then failed behaves.
    """

    answers: Sequence[Answer]
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        answer = self.answers[len(self.requests)]
        self.requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def bodies(self) -> list[object]:
        return [json.loads(request.content) for request in self.requests]


@dataclass
class RecordingSleep:
    """Records each wait and moves the fake clock forward by it."""

    now: datetime = NOW
    delays: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += timedelta(seconds=seconds)

    def clock(self) -> datetime:
        return self.now


@dataclass
class MessengerUnderTest:
    messenger: InstagramMessenger
    api: RecordingSendApi
    sleep: RecordingSleep


def build_messenger(
    answers: Sequence[Answer],
    max_attempts: int = MAX_ATTEMPTS,
    settings: MessengerSettings | None = None,
) -> MessengerUnderTest:
    """A messenger on a scripted Send API. Its clock only moves while it waits."""
    api = RecordingSendApi(answers)
    sleep = RecordingSleep()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api.handle))
    messenger = InstagramMessenger(
        client, settings or messenger_settings(max_attempts), sleep=sleep, clock=sleep.clock
    )
    return MessengerUnderTest(messenger, api, sleep)
