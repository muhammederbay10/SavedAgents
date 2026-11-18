import json
import logging
from datetime import datetime, timedelta

import httpx
import pytest

from app.agent.messenger import (
    MAX_QUICK_REPLIES,
    MAX_TEXT_BYTES,
    MessengerDeliveryUnknownError,
    MessengerRateLimitError,
    MessengerRequestError,
    MessengerResponseError,
    MessengerUnavailableError,
    OutgoingMessage,
    QuickReply,
    SendStatus,
)
from tests.agent.support import (
    ACCESS_TOKEN,
    BASE_DELAY_SECONDS,
    MAX_ATTEMPTS,
    MAX_DELAY_SECONDS,
    MESSAGES_URL,
    NOW,
    RECIPIENT_ID,
    SAVED_MESSAGE_ID,
    build_messenger,
    json_response,
    messenger_settings,
    rate_limited,
    sent,
)

RECENT = NOW - timedelta(minutes=5)
TEXT = "Saved: Inception (2010) · Sci-fi · on Netflix 🎬"


# --- success and request bodies -------------------------------------------------------------


async def test_text_message_is_posted_to_the_bot_account_with_the_token_in_a_header() -> None:
    under_test = build_messenger([sent()])

    result = await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert result.status is SendStatus.SENT
    assert result.message_id == "synthetic-sent-mid-0001"
    assert result.attempts == 1
    (request,) = under_test.api.requests
    assert request.method == "POST"
    assert str(request.url) == MESSAGES_URL
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert ACCESS_TOKEN not in str(request.url)
    assert under_test.api.bodies() == [
        {"recipient": {"id": RECIPIENT_ID}, "message": {"text": TEXT}}
    ]


async def test_reply_to_a_saved_reel_sends_exactly_recipient_message_and_reply_to() -> None:
    under_test = build_messenger([sent()])
    reply = OutgoingMessage(
        text="🍽 1/3 · Kronotrop, Beşiktaş · open now · https://maps.example/kronotrop",
        reply_to_message_id=SAVED_MESSAGE_ID,
    )

    await under_test.messenger.send(RECIPIENT_ID, reply, RECENT)

    assert under_test.api.bodies() == [
        {
            "recipient": {"id": RECIPIENT_ID},
            "message": {
                "text": "🍽 1/3 · Kronotrop, Beşiktaş · open now · https://maps.example/kronotrop"
            },
            "reply_to": {"mid": SAVED_MESSAGE_ID},
        }
    ]


async def test_quick_replies_are_sent_as_text_buttons_inside_the_message() -> None:
    under_test = build_messenger([sent()])
    question = OutgoingMessage(
        text="Which one did you save?",
        quick_replies=(
            QuickReply(title="1. Kronotrop Bebek", payload="choice:a"),
            QuickReply(title="None of these", payload="choice:none"),
        ),
    )

    await under_test.messenger.send(RECIPIENT_ID, question, RECENT)

    assert under_test.api.bodies() == [
        {
            "recipient": {"id": RECIPIENT_ID},
            "message": {
                "text": "Which one did you save?",
                "quick_replies": [
                    {"content_type": "text", "title": "1. Kronotrop Bebek", "payload": "choice:a"},
                    {"content_type": "text", "title": "None of these", "payload": "choice:none"},
                ],
            },
        }
    ]


# --- rate limits and retries -----------------------------------------------------------------


async def test_rate_limited_message_is_retried_with_doubling_backoff_and_then_sent() -> None:
    under_test = build_messenger([rate_limited(), rate_limited(), sent()])

    result = await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert result.status is SendStatus.SENT
    assert result.attempts == 3
    assert under_test.sleep.delays == [BASE_DELAY_SECONDS, BASE_DELAY_SECONDS * 2]
    first_body, *later_bodies = under_test.api.bodies()
    assert all(body == first_body for body in later_bodies)


async def test_http_429_is_retried_and_its_retry_after_is_honoured_up_to_the_maximum() -> None:
    under_test = build_messenger(
        [
            json_response("synthetic_send_success.json", 429, {"Retry-After": "2"}),
            json_response("synthetic_send_success.json", 429, {"Retry-After": "60"}),
            sent(),
        ]
    )

    result = await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert result.status is SendStatus.SENT
    assert under_test.sleep.delays == [2.0, MAX_DELAY_SECONDS]


async def test_rate_limit_on_every_attempt_raises_after_the_last_attempt() -> None:
    under_test = build_messenger([rate_limited()] * MAX_ATTEMPTS)

    with pytest.raises(MessengerRateLimitError) as raised:
        await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert raised.value.attempts == MAX_ATTEMPTS
    assert raised.value.error_code == 613
    assert raised.value.error_subcode == 2534040
    assert len(under_test.api.requests) == MAX_ATTEMPTS
    assert len(under_test.sleep.delays) == MAX_ATTEMPTS - 1


@pytest.mark.parametrize("error_code", [4, 17, 32, 613, 80002, 80006])
async def test_every_documented_throttling_code_is_retried(error_code: int) -> None:
    throttled = httpx.Response(400, json={"error": {"code": error_code, "type": "OAuthException"}})
    under_test = build_messenger([throttled, sent()])

    result = await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert result.attempts == 2


async def test_connection_that_never_opened_is_retried() -> None:
    under_test = build_messenger([httpx.ConnectError("refused"), sent()])

    result = await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert result.status is SendStatus.SENT
    assert result.attempts == 2


async def test_connection_that_never_opens_on_any_attempt_raises_unavailable() -> None:
    under_test = build_messenger([httpx.ConnectError("refused")] * MAX_ATTEMPTS)

    with pytest.raises(MessengerUnavailableError) as raised:
        await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert raised.value.status is None
    assert raised.value.attempts == MAX_ATTEMPTS


async def test_message_accepted_then_answered_with_503_is_delivered_once_and_not_retried() -> None:
    # The server took the message (it is recorded) and then answered 503. A
    # bare 503 doesn't show the message was not delivered, so it is not repeated.
    under_test = build_messenger([httpx.Response(503), sent()])

    with pytest.raises(MessengerDeliveryUnknownError) as raised:
        await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert len(under_test.api.requests) == 1
    assert under_test.sleep.delays == []
    assert raised.value.status == 503
    assert raised.value.attempts == 1


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ReadTimeout("slow"),
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(503, headers={"Retry-After": "1"}),
        httpx.Response(504),
    ],
    ids=["read_timeout", "http_500", "http_503", "http_503_retry_after", "http_504"],
)
async def test_failure_after_the_request_went_out_is_not_retried(
    answer: httpx.Response | Exception,
) -> None:
    under_test = build_messenger([answer, sent()])

    with pytest.raises(MessengerDeliveryUnknownError):
        await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert len(under_test.api.requests) == 1
    assert under_test.sleep.delays == []


async def test_refused_token_fails_at_once_with_its_error_code() -> None:
    under_test = build_messenger([json_response("synthetic_error_invalid_token.json", 400)])

    with pytest.raises(MessengerRequestError) as raised:
        await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert raised.value.error_code == 190
    assert raised.value.attempts == 1
    assert ACCESS_TOKEN not in str(raised.value)


async def test_ok_answer_without_a_message_id_is_an_error() -> None:
    under_test = build_messenger([httpx.Response(200, json={"recipient_id": RECIPIENT_ID})])

    with pytest.raises(MessengerResponseError):
        await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)


# --- the 24-hour reply window ----------------------------------------------------------------


async def test_message_outside_the_24_hour_window_is_skipped_without_a_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    under_test = build_messenger([sent()])
    too_old = NOW - timedelta(hours=24, seconds=1)

    with caplog.at_level(logging.INFO, logger="app.agent.messenger"):
        result = await under_test.messenger.send(
            RECIPIENT_ID, OutgoingMessage(text=TEXT, reply_to_message_id=SAVED_MESSAGE_ID), too_old
        )

    assert result.status is SendStatus.OUTSIDE_REPLY_WINDOW
    assert result.message_id is None
    assert result.attempts == 0
    assert under_test.api.requests == []
    (record,) = caplog.records
    assert record.getMessage() == "dm_skipped_outside_reply_window"
    assert record.__dict__["reason"] == "reply_window_elapsed"
    assert record.__dict__["hours_since_last_user_message"] == 24.0


async def test_message_at_exactly_24_hours_is_still_sent() -> None:
    under_test = build_messenger([sent()])

    result = await under_test.messenger.send(
        RECIPIENT_ID, OutgoingMessage(text=TEXT), NOW - timedelta(hours=24)
    )

    assert result.status is SendStatus.SENT


async def test_window_is_measured_across_time_zones() -> None:
    under_test = build_messenger([sent()])
    istanbul = datetime.fromisoformat("2026-11-18T14:30:00+03:00")  # 11:30 UTC, 30 min ago

    result = await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), istanbul)

    assert result.status is SendStatus.SENT


async def test_window_closing_during_backoff_stops_before_the_next_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    under_test = build_messenger([rate_limited(), sent()])
    # 0.25 s before the window closes; the first backoff waits 0.5 s.
    almost_expired = NOW - timedelta(hours=24) + timedelta(seconds=0.25)

    with caplog.at_level(logging.INFO, logger="app.agent.messenger"):
        result = await under_test.messenger.send(
            RECIPIENT_ID, OutgoingMessage(text=TEXT), almost_expired
        )

    assert under_test.sleep.delays == [BASE_DELAY_SECONDS]
    assert len(under_test.api.requests) == 1
    assert result.status is SendStatus.OUTSIDE_REPLY_WINDOW
    assert result.message_id is None
    assert result.attempts == 1
    skipped = [r for r in caplog.records if r.getMessage() == "dm_skipped_outside_reply_window"]
    assert [record.__dict__["attempts"] for record in skipped] == [1]
    assert skipped[0].__dict__["reason"] == "reply_window_elapsed"


async def test_configured_window_longer_than_instagram_allows_cannot_reach_the_api() -> None:
    # Validation refuses 48 hours; this also covers settings built without it.
    widened = messenger_settings().model_copy(update={"instagram_reply_window_hours": 48.0})
    under_test = build_messenger([sent()], settings=widened)

    result = await under_test.messenger.send(
        RECIPIENT_ID, OutgoingMessage(text=TEXT), NOW - timedelta(hours=25)
    )

    assert result.status is SendStatus.OUTSIDE_REPLY_WINDOW
    assert under_test.api.requests == []


async def test_meta_rejecting_a_closed_window_is_reported_as_skipped_and_not_retried() -> None:
    under_test = build_messenger([json_response("synthetic_error_outside_window.json", 400)])

    result = await under_test.messenger.send(RECIPIENT_ID, OutgoingMessage(text=TEXT), RECENT)

    assert result.status is SendStatus.OUTSIDE_REPLY_WINDOW
    assert result.attempts == 1
    assert len(under_test.api.requests) == 1


async def test_time_without_a_time_zone_is_refused() -> None:
    under_test = build_messenger([sent()])

    with pytest.raises(ValueError, match="time zone"):
        await under_test.messenger.send(
            RECIPIENT_ID, OutgoingMessage(text=TEXT), datetime(2026, 11, 18, 11, 0)
        )

    assert under_test.api.requests == []


# --- privacy ---------------------------------------------------------------------------------


async def test_logs_never_hold_the_recipient_message_ids_text_or_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    under_test = build_messenger(
        [rate_limited(), json_response("synthetic_error_invalid_token.json", 400)]
    )
    reply = OutgoingMessage(text=TEXT, reply_to_message_id=SAVED_MESSAGE_ID)

    with (
        caplog.at_level(logging.DEBUG, logger="app.agent.messenger"),
        pytest.raises(MessengerRequestError),
    ):
        await under_test.messenger.send(RECIPIENT_ID, reply, RECENT)

    logged = json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert [record.getMessage() for record in caplog.records] == [
        "dm_send_retrying",
        "dm_send_failed",
    ]
    for private in (RECIPIENT_ID, SAVED_MESSAGE_ID, "Inception", ACCESS_TOKEN, "OAuth"):
        assert private not in logged


# --- message validation ----------------------------------------------------------------------


def test_text_over_the_byte_limit_is_refused_even_when_short_in_characters() -> None:
    four_byte_emoji = "🎬"
    OutgoingMessage(text=four_byte_emoji * (MAX_TEXT_BYTES // 4))

    with pytest.raises(ValueError, match="bytes"):
        OutgoingMessage(text=four_byte_emoji * (MAX_TEXT_BYTES // 4 + 1))


def test_more_quick_replies_than_meta_allows_are_refused() -> None:
    buttons = tuple(QuickReply(title=str(n), payload=str(n)) for n in range(MAX_QUICK_REPLIES + 1))

    with pytest.raises(ValueError, match="quick replies"):
        OutgoingMessage(text="Which one?", quick_replies=buttons)


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="text"):
        OutgoingMessage(text=text)


def test_empty_reply_to_id_is_refused() -> None:
    with pytest.raises(ValueError, match="reply_to_message_id"):
        OutgoingMessage(text=TEXT, reply_to_message_id="")


async def test_empty_recipient_is_refused() -> None:
    under_test = build_messenger([sent()])

    with pytest.raises(ValueError, match="recipient_id"):
        await under_test.messenger.send(" ", OutgoingMessage(text=TEXT), RECENT)
