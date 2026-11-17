import base64
import json

import httpx
import pytest

from scripts.spikes.model_choices.budget import SpendCapExceededError, SpendLedger
from scripts.spikes.model_choices.openrouter import OpenRouterClient, build_http_client
from scripts.spikes.model_choices.settings import SpeechToTextModel

FAKE_KEY = "sk-test-not-a-real-key"
PLAIN_MODEL = SpeechToTextModel("vendor/plain", "json", 0.003)
VERBOSE_MODEL = SpeechToTextModel("vendor/verbose", "verbose_json", 0.003)


class Recorder:
    """A mock transport handler that keeps the requests it saw."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def client_for(recorder: Recorder, ledger: SpendLedger) -> OpenRouterClient:
    return OpenRouterClient(build_http_client(FAKE_KEY, httpx.MockTransport(recorder)), ledger)


def roomy_ledger() -> SpendLedger:
    return SpendLedger(cap_usd=2.0, spent_before_usd=0.0)


async def test_transcription_sends_base64_wav_and_the_models_format() -> None:
    recorder = Recorder(httpx.Response(200, json={"text": "hi", "usage": {"cost": 0.0002}}))
    ledger = roomy_ledger()

    result = await client_for(recorder, ledger).transcribe(PLAIN_MODEL, b"RIFFdata", 0.01)

    request = recorder.requests[0]
    assert request.url == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(request.content) == {
        "model": "vendor/plain",
        "input_audio": {"data": base64.b64encode(b"RIFFdata").decode("ascii"), "format": "wav"},
        "response_format": "json",
    }
    assert result.is_success
    assert result.returned_cost_usd == pytest.approx(0.0002)
    assert ledger.returned_usd == pytest.approx(0.0002)


async def test_verbose_transcription_asks_for_word_and_segment_timestamps() -> None:
    recorder = Recorder(httpx.Response(200, json={"text": "hi", "usage": {"cost": 0.0002}}))

    await client_for(recorder, roomy_ledger()).transcribe(VERBOSE_MODEL, b"RIFFdata", 0.01)

    body = json.loads(recorder.requests[0].content)
    assert body["response_format"] == "verbose_json"
    assert body["timestamp_granularities"] == ["word", "segment"]


async def test_embedding_sends_dimensions_only_for_the_reduced_size() -> None:
    recorder = Recorder(httpx.Response(200, json={"data": [], "usage": {"cost": 0.0}}))
    client = client_for(recorder, roomy_ledger())

    await client.embed("vendor/embedder", ["one", "two"], 1024, 0.01)
    await client.embed("vendor/embedder", ["one", "two"], None, 0.01)

    reduced, full = (json.loads(request.content) for request in recorder.requests)
    assert recorder.requests[0].url == "https://openrouter.ai/api/v1/embeddings"
    assert reduced == {"model": "vendor/embedder", "input": ["one", "two"], "dimensions": 1024}
    assert full == {"model": "vendor/embedder", "input": ["one", "two"]}


async def test_request_over_the_cap_is_never_sent() -> None:
    recorder = Recorder(httpx.Response(200, json={}))
    ledger = SpendLedger(cap_usd=2.0, spent_before_usd=1.99)

    with pytest.raises(SpendCapExceededError):
        await client_for(recorder, ledger).transcribe(PLAIN_MODEL, b"RIFFdata", 0.02)

    assert recorder.requests == []


async def test_error_response_is_returned_and_counted_at_its_estimate() -> None:
    recorder = Recorder(httpx.Response(429, json={"error": {"message": "slow down"}}))
    ledger = roomy_ledger()

    result = await client_for(recorder, ledger).transcribe(PLAIN_MODEL, b"RIFFdata", 0.01)

    assert result.status == 429
    assert not result.is_success
    assert result.returned_cost_usd is None
    assert ledger.unconfirmed_usd == pytest.approx(0.01)


async def test_timeout_is_recorded_as_a_failure_without_a_status() -> None:
    recorder = Recorder(httpx.ReadTimeout("timed out"))
    ledger = roomy_ledger()

    result = await client_for(recorder, ledger).transcribe(PLAIN_MODEL, b"RIFFdata", 0.01)

    assert result.status is None
    assert result.error == "ReadTimeout"
    assert ledger.unconfirmed_usd == pytest.approx(0.01)


async def test_non_json_response_has_an_empty_body() -> None:
    recorder = Recorder(httpx.Response(502, text="bad gateway"))

    result = await client_for(recorder, roomy_ledger()).transcribe(PLAIN_MODEL, b"RIFFdata", 0.01)

    assert result.status == 502
    assert result.body == {}


async def test_key_usage_is_read_without_touching_the_ledger() -> None:
    recorder = Recorder(httpx.Response(200, json={"data": {"usage": 0.08541, "limit": None}}))
    ledger = roomy_ledger()

    usage = await client_for(recorder, ledger).key_usage_usd()

    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url == "https://openrouter.ai/api/v1/key"
    assert usage == pytest.approx(0.08541)
    assert ledger.cumulative_usd == 0.0


async def test_unreadable_key_usage_is_none() -> None:
    recorder = Recorder(httpx.ConnectError("no network"))

    assert await client_for(recorder, roomy_ledger()).key_usage_usd() is None


@pytest.mark.parametrize(
    "cost_literal",
    [
        pytest.param("NaN", id="NaN"),
        pytest.param("Infinity", id="infinity"),
        pytest.param("-Infinity", id="minus infinity"),
        pytest.param("1e999", id="overflows to infinity"),
        pytest.param("-0.5", id="negative"),
        pytest.param('"0.0002"', id="a string"),
        pytest.param("true", id="a boolean"),
    ],
)
async def test_malformed_charge_is_dropped_and_the_estimate_stays_reserved(
    cost_literal: str,
) -> None:
    body = f'{{"text": "hi", "usage": {{"cost": {cost_literal}}}}}'
    recorder = Recorder(httpx.Response(200, content=body.encode("utf-8")))
    ledger = SpendLedger(cap_usd=1.0, spent_before_usd=0.0)
    client = client_for(recorder, ledger)

    result = await client.transcribe(PLAIN_MODEL, b"RIFFdata", 0.6)

    assert result.is_success
    assert result.returned_cost_usd is None
    assert ledger.returned_usd == 0.0
    assert ledger.unconfirmed_usd == pytest.approx(0.6)
    with pytest.raises(SpendCapExceededError):
        await client.transcribe(PLAIN_MODEL, b"RIFFdata", 0.5)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("usage_literal", ["NaN", "Infinity", "-1"])
async def test_malformed_key_usage_is_none(usage_literal: str) -> None:
    body = f'{{"data": {{"usage": {usage_literal}}}}}'
    recorder = Recorder(httpx.Response(200, content=body.encode("utf-8")))

    assert await client_for(recorder, roomy_ledger()).key_usage_usd() is None
