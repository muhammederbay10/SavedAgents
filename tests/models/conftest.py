"""Fixtures for the model-client tests: an OpenRouter transport over a mock."""

from collections.abc import AsyncIterator, Callable, Sequence

import httpx
import pytest

from app.models.openrouter.transport import OpenRouterTransport, RetryPolicy, build_http_client
from tests.models.support import SYNTHETIC_API_KEY, FakeOpenRouter, SleepRecorder

MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 4.0

Connect = Callable[
    [Sequence[httpx.Response | Exception]], tuple[FakeOpenRouter, OpenRouterTransport]
]


@pytest.fixture
def sleep_recorder() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def connect(sleep_recorder: SleepRecorder) -> AsyncIterator[Connect]:
    """Build a transport whose requests are answered by scripted responses."""
    http_clients: list[httpx.AsyncClient] = []

    def _connect(
        responses: Sequence[httpx.Response | Exception],
    ) -> tuple[FakeOpenRouter, OpenRouterTransport]:
        server = FakeOpenRouter(responses)
        http_client = build_http_client(SYNTHETIC_API_KEY, httpx.MockTransport(server.handle))
        http_clients.append(http_client)
        retry_policy = RetryPolicy(
            max_attempts=MAX_ATTEMPTS,
            base_delay_seconds=RETRY_BASE_DELAY_SECONDS,
            max_delay_seconds=RETRY_MAX_DELAY_SECONDS,
        )
        return server, OpenRouterTransport(http_client, retry_policy, sleep=sleep_recorder)

    yield _connect
    for http_client in http_clients:
        await http_client.aclose()
