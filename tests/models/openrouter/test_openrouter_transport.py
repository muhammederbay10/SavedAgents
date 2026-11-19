import logging

import httpx
import pytest

from app.models.errors import (
    ModelAuthenticationError,
    ModelCreditsError,
    ModelError,
    ModelRateLimitError,
    ModelRequestError,
    ModelResponseError,
    ModelUnavailableError,
)
from app.models.openrouter.transport import ModelCall
from tests.models.conftest import (
    MAX_ATTEMPTS,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_MAX_DELAY_SECONDS,
    Connect,
)
from tests.models.support import (
    EMBEDDINGS_URL,
    SYNTHETIC_API_KEY,
    TIMEOUT_SECONDS,
    SleepRecorder,
    error_response,
    fixture_response,
    json_response,
)

CALL = ModelCall(
    operation="embed", model="test/model", url=EMBEDDINGS_URL, timeout_seconds=TIMEOUT_SECONDS
)
PRIVATE_CAPTION = "synthetic private caption 7c1d"
PAYLOAD: dict[str, object] = {"model": "test/model", "input": [PRIVATE_CAPTION]}
SUCCESS_BODY = {"data": []}


async def test_request_carries_the_key_the_payload_and_the_calls_timeout(
    connect: Connect,
) -> None:
    server, transport = connect([json_response(SUCCESS_BODY)])

    body = await transport.post(CALL, PAYLOAD)

    assert body == SUCCESS_BODY
    assert server.only_request.url == EMBEDDINGS_URL
    assert server.only_request.authorization == f"Bearer {SYNTHETIC_API_KEY}"
    assert server.only_request.timeout_seconds == TIMEOUT_SECONDS
    assert server.only_request.body == PAYLOAD


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 524, 529])
async def test_transient_status_is_retried_until_it_succeeds(
    connect: Connect, sleep_recorder: SleepRecorder, status: int
) -> None:
    server, transport = connect([error_response(status), json_response(SUCCESS_BODY)])

    assert await transport.post(CALL, PAYLOAD) == SUCCESS_BODY
    assert len(server.requests) == 2
    assert sleep_recorder.delays == [RETRY_BASE_DELAY_SECONDS]


@pytest.mark.parametrize(
    "network_error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut")],
)
async def test_network_failure_is_retried(
    connect: Connect, network_error: httpx.TransportError
) -> None:
    server, transport = connect([network_error, json_response(SUCCESS_BODY)])

    assert await transport.post(CALL, PAYLOAD) == SUCCESS_BODY
    assert len(server.requests) == 2


async def test_wait_doubles_between_attempts(
    connect: Connect, sleep_recorder: SleepRecorder
) -> None:
    _, transport = connect([error_response(503)] * MAX_ATTEMPTS)

    with pytest.raises(ModelUnavailableError):
        await transport.post(CALL, PAYLOAD)

    assert sleep_recorder.delays == [RETRY_BASE_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * 2]


async def test_retry_after_header_lengthens_the_wait_up_to_the_maximum(
    connect: Connect, sleep_recorder: SleepRecorder
) -> None:
    _, transport = connect(
        [
            json_response({"error": {"code": 429}}, status=429, headers={"Retry-After": "2"}),
            json_response({"error": {"code": 429}}, status=429, headers={"Retry-After": "600"}),
            json_response(SUCCESS_BODY),
        ]
    )

    await transport.post(CALL, PAYLOAD)

    assert sleep_recorder.delays == [2.0, RETRY_MAX_DELAY_SECONDS]


async def test_rate_limit_that_never_clears_raises_rate_limit_error(connect: Connect) -> None:
    server, transport = connect([fixture_response_429()] * MAX_ATTEMPTS)

    with pytest.raises(ModelRateLimitError) as raised:
        await transport.post(CALL, PAYLOAD)

    assert len(server.requests) == MAX_ATTEMPTS
    assert raised.value.status == 429
    assert raised.value.error_type == "rate_limit_exceeded"
    assert raised.value.operation == "embed"
    assert raised.value.model == "test/model"


def fixture_response_429() -> httpx.Response:
    return httpx.Response(429, json=fixture_response("error_rate_limited.json").json())


async def test_network_failure_on_every_attempt_raises_unavailable(connect: Connect) -> None:
    _, transport = connect([httpx.ConnectTimeout("slow")] * MAX_ATTEMPTS)

    with pytest.raises(ModelUnavailableError, match="no response \\(ConnectTimeout\\)") as raised:
        await transport.post(CALL, PAYLOAD)

    assert raised.value.status is None


@pytest.mark.parametrize(
    ("status", "error_class"),
    [
        (400, ModelRequestError),
        (401, ModelAuthenticationError),
        (402, ModelCreditsError),
        (403, ModelRequestError),
        (404, ModelRequestError),
        (413, ModelRequestError),
        (422, ModelRequestError),
    ],
)
async def test_rejected_request_fails_at_once_with_its_own_error_type(
    connect: Connect, sleep_recorder: SleepRecorder, status: int, error_class: type[ModelError]
) -> None:
    server, transport = connect([error_response(status)])

    with pytest.raises(error_class) as raised:
        await transport.post(CALL, PAYLOAD)

    assert type(raised.value) is error_class
    assert raised.value.status == status
    assert len(server.requests) == 1
    assert sleep_recorder.delays == []


async def test_error_object_inside_an_http_200_is_an_error_not_an_answer(
    connect: Connect,
) -> None:
    _, transport = connect([json_response({"error": {"code": 400, "message": "bad"}})])

    with pytest.raises(ModelRequestError) as raised:
        await transport.post(CALL, PAYLOAD)

    assert raised.value.status == 400


async def test_transient_error_inside_an_http_200_is_retried(connect: Connect) -> None:
    server, transport = connect(
        [json_response({"error": {"code": 502, "message": "down"}}), json_response(SUCCESS_BODY)]
    )

    assert await transport.post(CALL, PAYLOAD) == SUCCESS_BODY
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"error": {"message": "no code"}}),
    ],
)
async def test_unreadable_success_response_raises_response_error_without_retrying(
    connect: Connect, response: httpx.Response
) -> None:
    server, transport = connect([response])

    with pytest.raises(ModelResponseError):
        await transport.post(CALL, PAYLOAD)

    assert len(server.requests) == 1


async def test_error_label_that_is_not_a_short_identifier_is_dropped(connect: Connect) -> None:
    quoted_input = f"could not parse: {PRIVATE_CAPTION}"
    _, transport = connect([error_response(400, error_type=quoted_input)])

    with pytest.raises(ModelRequestError) as raised:
        await transport.post(CALL, PAYLOAD)

    assert raised.value.error_type is None


async def test_logs_and_errors_never_carry_the_key_or_any_body(
    connect: Connect, caplog: pytest.LogCaptureFixture
) -> None:
    upstream_message = f"provider echoed: {PRIVATE_CAPTION}"
    _, transport = connect(
        [
            json_response({"error": {"code": 503, "message": upstream_message}}, status=503),
            json_response({"data": [PRIVATE_CAPTION]}),
            json_response({"error": {"code": 400, "message": upstream_message}}, status=400),
        ]
    )

    with caplog.at_level(logging.DEBUG):
        await transport.post(CALL, PAYLOAD)
        with pytest.raises(ModelRequestError) as raised:
            await transport.post(CALL, PAYLOAD)

    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    events = [record.getMessage() for record in caplog.records if record.name.startswith("app.")]
    assert events == ["model_call_retrying", "model_call_succeeded", "model_call_failed"]
    for secret in (PRIVATE_CAPTION, SYNTHETIC_API_KEY, "synthetic error message"):
        assert secret not in logged
        assert secret not in str(raised.value)
