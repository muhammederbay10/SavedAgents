"""The webhook endpoints: handshake, signature check and routing, without a database.

Ingestion is replaced by a recorder, so these tests show what the endpoint hands
on. What ingestion then stores and queues is checked in
`tests/test_ingestion_service.py` and `tests/test_webhook_ingestion.py`.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.base import CaptureEvent, ChatMessage
from app.api.webhooks import create_instagram_webhook_router
from app.config import Settings
from app.ingestion.service import IngestionOutcome
from app.main import create_app
from tests.conftest import TEST_META_APP_SECRET, TEST_META_WEBHOOK_VERIFY_TOKEN
from tests.webhooks import (
    DASHBOARD_SAMPLE,
    HINT_AFTER_REEL,
    REEL_SHARE,
    SHARE_FIXTURES,
    SIGNATURE_HEADER,
    TEXT_FIXTURES,
    TEXT_QUESTION,
    body_of,
    fixture_body,
    fixture_json,
    fixture_message,
    signature_for,
    signed_headers,
)

WEBHOOK_PATH = "/webhooks/instagram"
SENDER_ID = "1000000000000001"
USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
CHALLENGE = "1158201444"
HANDSHAKE = {
    "hub.mode": "subscribe",
    "hub.verify_token": TEST_META_WEBHOOK_VERIFY_TOKEN,
    "hub.challenge": CHALLENGE,
}


@dataclass
class RecordingIngestion:
    """Stands in for `IngestionService` and remembers what the endpoint handed it."""

    resolved_accounts: list[tuple[str, str]] = field(default_factory=list)
    captures: list[CaptureEvent] = field(default_factory=list)
    chat_messages: list[ChatMessage] = field(default_factory=list)

    async def resolve_user(self, platform: str, platform_user_id: str) -> uuid.UUID:
        self.resolved_accounts.append((platform, platform_user_id))
        return USER_ID

    async def ingest_capture(self, event: CaptureEvent) -> IngestionOutcome:
        self.captures.append(event)
        return IngestionOutcome.ITEM_SAVED

    async def ingest_chat_message(self, message: ChatMessage) -> IngestionOutcome:
        self.chat_messages.append(message)
        return IngestionOutcome.CHAT_QUEUED

    @property
    def received_nothing(self) -> bool:
        return not (self.resolved_accounts or self.captures or self.chat_messages)


class UnreachableIngestion(RecordingIngestion):
    """An ingestion whose database is down."""

    async def resolve_user(self, platform: str, platform_user_id: str) -> uuid.UUID:
        raise ConnectionError("the database is unreachable")


def webhook_client(settings: Settings, ingestion: RecordingIngestion) -> AsyncClient:
    app = FastAPI()
    app.include_router(create_instagram_webhook_router(settings, ingestion))
    # A failing handler must answer 500, as a real server does, instead of raising here.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
def ingestion() -> RecordingIngestion:
    return RecordingIngestion()


@pytest.fixture
async def webhooks(settings: Settings, ingestion: RecordingIngestion) -> AsyncIterator[AsyncClient]:
    async with webhook_client(settings, ingestion) as http_client:
        yield http_client


# --- Verification handshake --------------------------------------------------


async def test_handshake_with_our_verify_token_returns_the_challenge(
    webhooks: AsyncClient,
) -> None:
    response = await webhooks.get(WEBHOOK_PATH, params=HANDSHAKE)

    assert response.status_code == 200
    assert response.text == CHALLENGE
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "changed",
    [
        {"hub.verify_token": "another-token"},
        {"hub.verify_token": ""},
        {"hub.mode": "unsubscribe"},
    ],
)
async def test_handshake_with_a_wrong_token_or_mode_is_refused(
    webhooks: AsyncClient, changed: dict[str, str]
) -> None:
    response = await webhooks.get(WEBHOOK_PATH, params={**HANDSHAKE, **changed})

    assert response.status_code == 403
    assert CHALLENGE not in response.text


@pytest.mark.parametrize("missing", sorted(HANDSHAKE))
async def test_handshake_with_a_missing_parameter_is_refused(
    webhooks: AsyncClient, missing: str
) -> None:
    params = {name: value for name, value in HANDSHAKE.items() if name != missing}

    response = await webhooks.get(WEBHOOK_PATH, params=params)

    assert response.status_code == 403


# --- Signature ---------------------------------------------------------------


async def test_webhook_with_a_valid_signature_is_acknowledged_with_200(
    webhooks: AsyncClient, ingestion: RecordingIngestion
) -> None:
    body = fixture_body(REEL_SHARE)

    response = await webhooks.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))

    assert response.status_code == 200
    assert len(ingestion.captures) == 1


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "sha256=" + "0" * 64,
        signature_for(fixture_body(REEL_SHARE), app_secret="another-synthetic-secret"),
        signature_for(fixture_body(TEXT_QUESTION)),
    ],
    ids=["missing", "empty", "wrong digest", "other secret", "signature of another body"],
)
async def test_webhook_with_an_invalid_signature_is_refused_and_nothing_is_ingested(
    webhooks: AsyncClient, ingestion: RecordingIngestion, signature: str | None
) -> None:
    headers = {} if signature is None else {SIGNATURE_HEADER: signature}

    response = await webhooks.post(WEBHOOK_PATH, content=fixture_body(REEL_SHARE), headers=headers)

    assert response.status_code == 401
    assert ingestion.received_nothing


async def test_signature_is_checked_against_the_bytes_that_were_sent(
    webhooks: AsyncClient, ingestion: RecordingIngestion
) -> None:
    recorded_body = fixture_body(REEL_SHARE)
    same_json_other_bytes = body_of(fixture_json(REEL_SHARE))

    response = await webhooks.post(
        WEBHOOK_PATH, content=same_json_other_bytes, headers=signed_headers(recorded_body)
    )

    assert same_json_other_bytes != recorded_body
    assert response.status_code == 401
    assert ingestion.received_nothing


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"object": "instagram"}'])
async def test_signed_body_that_is_not_a_webhook_object_is_refused(
    webhooks: AsyncClient, ingestion: RecordingIngestion, body: bytes
) -> None:
    response = await webhooks.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))

    assert response.status_code == 400
    assert ingestion.received_nothing


# --- Routing -----------------------------------------------------------------


@pytest.mark.parametrize("name", SHARE_FIXTURES)
async def test_every_share_fixture_is_handed_on_as_a_capture_with_its_message_id(
    webhooks: AsyncClient, ingestion: RecordingIngestion, name: str
) -> None:
    body = fixture_body(name)

    response = await webhooks.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))

    assert response.status_code == 200
    (capture,) = ingestion.captures
    assert capture.user_id == USER_ID
    assert capture.platform == "instagram"
    assert capture.source == "dm"
    assert capture.source_message_id == fixture_message(name)["mid"]
    assert ingestion.chat_messages == []
    assert ingestion.resolved_accounts == [("instagram", SENDER_ID)]


@pytest.mark.parametrize("name", TEXT_FIXTURES)
async def test_every_text_fixture_is_handed_on_as_a_chat_message(
    webhooks: AsyncClient, ingestion: RecordingIngestion, name: str
) -> None:
    body = fixture_body(name)

    response = await webhooks.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))

    assert response.status_code == 200
    (message,) = ingestion.chat_messages
    assert message.user_id == USER_ID
    assert message.text == fixture_message(name)["text"]
    assert message.message_id == fixture_message(name)["mid"]
    assert ingestion.captures == []
    assert ingestion.resolved_accounts == [("instagram", SENDER_ID)]


async def test_dashboard_test_sample_is_acknowledged_and_ingests_nothing(
    webhooks: AsyncClient, ingestion: RecordingIngestion
) -> None:
    body = fixture_body(DASHBOARD_SAMPLE)

    response = await webhooks.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))

    assert response.status_code == 200
    assert ingestion.received_nothing


async def test_webhook_with_several_messages_ingests_each_in_order(
    webhooks: AsyncClient, ingestion: RecordingIngestion
) -> None:
    payload = fixture_json(REEL_SHARE)
    payload["entry"][0]["messaging"].append(
        fixture_json(HINT_AFTER_REEL)["entry"][0]["messaging"][0]
    )
    body = body_of(payload)

    response = await webhooks.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))

    assert response.status_code == 200
    assert len(ingestion.captures) == 1
    assert [message.text for message in ingestion.chat_messages] == ["For my movies collection"]


async def test_failing_ingestion_answers_500_so_meta_delivers_the_webhook_again(
    settings: Settings,
) -> None:
    body = fixture_body(REEL_SHARE)

    async with webhook_client(settings, UnreachableIngestion()) as http_client:
        response = await http_client.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))

    assert response.status_code == 500


# --- Privacy -----------------------------------------------------------------


@pytest.mark.parametrize("name", [REEL_SHARE, TEXT_QUESTION])
async def test_webhook_logs_hold_no_message_content_ids_or_secrets(
    webhooks: AsyncClient, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    body = fixture_body(name)
    message = fixture_message(name)
    private_values = [
        SENDER_ID,
        message["mid"],
        message.get("text", "FAKEREEL002"),
        "Kadıköy",
        TEST_META_APP_SECRET,
        signature_for(body),
    ]

    with caplog.at_level(logging.DEBUG):
        await webhooks.post(WEBHOOK_PATH, content=body, headers=signed_headers(body))
        await webhooks.post(WEBHOOK_PATH, content=body, headers={SIGNATURE_HEADER: "sha256=00"})

    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert "instagram_webhook_signature_rejected" in logged
    assert [value for value in private_values if value in logged] == []


# --- The real app ------------------------------------------------------------


async def test_app_serves_the_handshake(client: AsyncClient) -> None:
    response = await client.get(WEBHOOK_PATH, params=HANDSHAKE)

    assert response.status_code == 200
    assert response.text == CHALLENGE


async def test_app_refuses_an_unsigned_webhook_before_touching_postgres_or_redis(
    client: AsyncClient,
) -> None:
    response = await client.post(WEBHOOK_PATH, content=fixture_body(REEL_SHARE))

    assert response.status_code == 401


async def test_app_shuts_down_cleanly_when_it_never_reached_postgres_or_redis(
    settings: Settings,
) -> None:
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        pass
