"""A stand-in for OpenRouter. No test in this folder reaches the network."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.models.openrouter.transport import (
    CHAT_COMPLETIONS_PATH,
    EMBEDDINGS_PATH,
    TRANSCRIPTIONS_PATH,
    endpoint_url,
)

FIXTURES_DIRECTORY = Path(__file__).resolve().parent.parent / "fixtures" / "openrouter"

BASE_URL = "https://openrouter.test/api/v1"
DECISIONS_URL = "https://openrouter.test/api/alpha/decisions"
CHAT_COMPLETIONS_URL = endpoint_url(BASE_URL, CHAT_COMPLETIONS_PATH)
TRANSCRIPTIONS_URL = endpoint_url(BASE_URL, TRANSCRIPTIONS_PATH)
EMBEDDINGS_URL = endpoint_url(BASE_URL, EMBEDDINGS_PATH)

SYNTHETIC_API_KEY = "synthetic-key-3b1f"
TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class RecordedRequest:
    """One request the stand-in received."""

    url: str
    authorization: str | None
    routing_metadata_header: str | None
    timeout_seconds: float | None
    body: dict[str, Any]


class FakeOpenRouter:
    """Answers scripted responses in order and records every request."""

    def __init__(self, responses: Sequence[httpx.Response | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[RecordedRequest] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            RecordedRequest(
                url=str(request.url),
                authorization=request.headers.get("Authorization"),
                routing_metadata_header=request.headers.get("X-OpenRouter-Metadata"),
                timeout_seconds=request.extensions["timeout"].get("read"),
                body=json.loads(request.content),
            )
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def script(self, *responses: httpx.Response | Exception) -> None:
        """Add responses to answer the next requests with."""
        self._responses.extend(responses)

    @property
    def only_request(self) -> RecordedRequest:
        assert len(self.requests) == 1
        return self.requests[0]


class SleepRecorder:
    """Records the waits between attempts without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def load_fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES_DIRECTORY / name).read_text(encoding="utf-8"))
    return body


def json_response(
    body: object, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def fixture_response(name: str) -> httpx.Response:
    return json_response(load_fixture(name))


def error_response(status: int, error_type: str | None = None) -> httpx.Response:
    error: dict[str, object] = {"code": status, "message": "synthetic error message"}
    if error_type is not None:
        error["metadata"] = {"error_type": error_type}
    return json_response({"error": error}, status=status)
