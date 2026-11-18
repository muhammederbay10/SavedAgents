"""The Instagram adapter: signature check and mapping of the recorded webhooks."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.adapters.instagram import (
    InstagramShare,
    InstagramText,
    InvalidWebhookPayloadError,
    is_valid_signature,
    parse_webhook,
    to_capture_event,
    to_chat_message,
)
from tests.conftest import TEST_META_APP_SECRET
from tests.webhooks import (
    CAROUSEL_POST_SHARE,
    COLLECTION_PHOTO,
    COLLECTION_REEL,
    COLLECTION_REEL_WITH_CAPTION,
    DASHBOARD_SAMPLE,
    HINT_AFTER_REEL,
    LEGACY_SHARE,
    OTHER_COLLECTION_REEL,
    PHOTO_POST_SHARE,
    REEL_SHARE,
    SHARE_FIXTURES,
    TEXT_FIXTURES,
    TEXT_QUESTION,
    body_of,
    fixture_body,
    fixture_json,
    fixture_message,
    signature_for,
)

SENDER_ID = "1000000000000001"
PUBLIC_REEL_HOST = "www.instagram.com"
SIGNED_MEDIA_HOST = "lookaside.fbsbx.com"
USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def only_share(name: str) -> InstagramShare:
    (message,) = parse_webhook(fixture_body(name))
    assert isinstance(message, InstagramShare)
    return message


def only_text(name: str) -> InstagramText:
    (message,) = parse_webhook(fixture_body(name))
    assert isinstance(message, InstagramText)
    return message


def attachment_payload(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = fixture_message(name)["attachments"][0]["payload"]
    return payload


def webhook_with_message(message: dict[str, Any], **event_fields: object) -> bytes:
    """A webhook in the recorded shape, holding one synthetic message."""
    payload = fixture_json(TEXT_QUESTION)
    event = payload["entry"][0]["messaging"][0]
    event["message"] = message
    event.update(event_fields)
    return body_of(payload)


def webhook_with_events(*events: dict[str, Any]) -> bytes:
    payload = fixture_json(TEXT_QUESTION)
    payload["entry"][0]["messaging"] = list(events)
    return body_of(payload)


def recorded_event(name: str) -> dict[str, Any]:
    event: dict[str, Any] = fixture_json(name)["entry"][0]["messaging"][0]
    return event


# --- Signature ---------------------------------------------------------------


def test_signature_of_the_raw_body_is_accepted() -> None:
    body = fixture_body(REEL_SHARE)

    assert is_valid_signature(body, signature_for(body), TEST_META_APP_SECRET)


def test_signature_made_with_another_secret_is_rejected() -> None:
    body = fixture_body(REEL_SHARE)
    forged = signature_for(body, app_secret="another-synthetic-secret")

    assert not is_valid_signature(body, forged, TEST_META_APP_SECRET)


def test_signature_is_rejected_when_the_body_was_changed() -> None:
    body = fixture_body(REEL_SHARE)
    changed_body = body.replace(b"FAKEREEL002", b"FAKEREEL999")

    assert not is_valid_signature(changed_body, signature_for(body), TEST_META_APP_SECRET)


def test_signature_of_the_same_json_in_another_layout_is_rejected() -> None:
    body = fixture_body(REEL_SHARE)
    reserialized = body_of(fixture_json(REEL_SHARE))

    assert reserialized != body
    assert not is_valid_signature(reserialized, signature_for(body), TEST_META_APP_SECRET)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "sha1=0000000000000000000000000000000000000000",
        "not-a-signature",
        "sha256=ğüş-not-hex",
    ],
)
def test_missing_or_malformed_signature_header_is_rejected(header: str | None) -> None:
    assert not is_valid_signature(fixture_body(REEL_SHARE), header, TEST_META_APP_SECRET)


def test_signature_without_the_sha256_prefix_is_rejected() -> None:
    body = fixture_body(REEL_SHARE)
    bare_digest = signature_for(body).removeprefix("sha256=")

    assert not is_valid_signature(body, bare_digest, TEST_META_APP_SECRET)


# --- Shares ------------------------------------------------------------------


def test_reel_shared_by_dm_keeps_its_id_caption_and_public_link() -> None:
    share = only_share(REEL_SHARE)
    payload = attachment_payload(REEL_SHARE)

    assert share.media_type == "reel"
    assert share.platform_post_id == payload["reel_video_id"]
    assert share.post_url == payload["url"]
    assert urlsplit(share.post_url).hostname == PUBLIC_REEL_HOST
    assert share.media_url is None
    assert share.caption == payload["title"]
    assert "\n" in share.caption
    assert share.sender_id == SENDER_ID


def test_photo_post_shared_by_dm_has_a_media_link_and_no_post_link() -> None:
    share = only_share(PHOTO_POST_SHARE)
    payload = attachment_payload(PHOTO_POST_SHARE)

    assert share.media_type == "post"
    assert share.platform_post_id == payload["ig_post_media_id"]
    assert share.post_url is None
    assert share.media_url == payload["url"]
    assert share.caption == "Best brunch spot"


def test_carousel_arrives_as_one_post_without_a_caption() -> None:
    share = only_share(CAROUSEL_POST_SHARE)

    assert share.media_type == "post"
    assert share.platform_post_id == attachment_payload(CAROUSEL_POST_SHARE)["ig_post_media_id"]
    assert share.caption is None


@pytest.mark.parametrize(
    "name", [COLLECTION_REEL, OTHER_COLLECTION_REEL, COLLECTION_REEL_WITH_CAPTION]
)
def test_reel_saved_into_a_collection_has_only_a_message_id_and_a_video_link(name: str) -> None:
    share = only_share(name)

    assert share.media_type == "reel"
    assert share.platform_post_id is None
    assert share.post_url is None
    assert share.caption is None
    assert share.media_url == attachment_payload(name)["url"]
    assert share.message_id == fixture_message(name)["mid"]


def test_photo_saved_into_a_collection_maps_like_a_photo_shared_by_dm() -> None:
    share = only_share(COLLECTION_PHOTO)
    payload = attachment_payload(COLLECTION_PHOTO)

    assert share.media_type == "post"
    assert share.platform_post_id == payload["ig_post_media_id"]
    assert share.post_url is None
    assert share.media_url == payload["url"]
    assert share.caption == payload["title"]


def test_legacy_share_attachment_is_a_post_with_a_media_link() -> None:
    share = only_share(LEGACY_SHARE)

    assert share.media_type == "post"
    assert share.platform_post_id is None
    assert share.post_url is None
    assert share.media_url == attachment_payload(LEGACY_SHARE)["url"]
    assert share.caption is None


def test_legacy_share_with_a_public_instagram_link_keeps_it_as_the_post_link() -> None:
    message = fixture_message(LEGACY_SHARE)
    message["attachments"][0]["payload"]["url"] = "https://www.instagram.com/p/FAKEPOST001/"

    (share,) = parse_webhook(webhook_with_message(message))

    assert isinstance(share, InstagramShare)
    assert share.post_url == "https://www.instagram.com/p/FAKEPOST001/"
    assert share.media_url is None


@pytest.mark.parametrize("name", SHARE_FIXTURES)
def test_signed_media_link_is_never_the_post_link(name: str) -> None:
    share = only_share(name)

    assert share.post_url is None or urlsplit(share.post_url).hostname == PUBLIC_REEL_HOST
    assert share.media_url is None or urlsplit(share.media_url).hostname == SIGNED_MEDIA_HOST
    assert share.post_url is not None or share.media_url is not None


def test_reel_whose_link_is_a_signed_file_keeps_it_out_of_the_post_link() -> None:
    message = fixture_message(REEL_SHARE)
    signed_link = attachment_payload(COLLECTION_REEL)["url"]
    message["attachments"][0]["payload"]["url"] = signed_link

    (share,) = parse_webhook(webhook_with_message(message))

    assert isinstance(share, InstagramShare)
    assert share.post_url is None
    assert share.media_url == signed_link


@pytest.mark.parametrize("name", SHARE_FIXTURES)
def test_every_share_keeps_its_message_id_and_send_time(name: str) -> None:
    share = only_share(name)
    event = recorded_event(name)

    assert share.message_id == event["message"]["mid"]
    assert share.sent_at == datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC)
    assert share.sent_at.tzinfo is UTC


def test_message_time_is_read_as_milliseconds() -> None:
    share = only_share(REEL_SHARE)

    assert share.sent_at == datetime(2026, 1, 15, 12, 0, 56, 454000, tzinfo=UTC)


@pytest.mark.parametrize("missing", ["", "   "])
def test_blank_id_link_or_caption_is_missing_not_empty_text(missing: str) -> None:
    message = fixture_message(REEL_SHARE)
    message["attachments"][0]["payload"].update(reel_video_id=missing, title=missing)

    (share,) = parse_webhook(webhook_with_message(message))

    assert isinstance(share, InstagramShare)
    assert share.platform_post_id is None
    assert share.caption is None


def test_message_with_two_shares_captures_the_first_and_logs_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = fixture_message(REEL_SHARE)
    message["attachments"].append(fixture_message(PHOTO_POST_SHARE)["attachments"][0])

    with caplog.at_level(logging.WARNING):
        (share,) = parse_webhook(webhook_with_message(message))

    assert isinstance(share, InstagramShare)
    assert share.media_type == "reel"
    (record,) = [r for r in caplog.records if r.message == "instagram_extra_shares_ignored"]
    assert record.__dict__["ignored_count"] == 1


# --- Texts -------------------------------------------------------------------


@pytest.mark.parametrize("name", TEXT_FIXTURES)
def test_plain_text_becomes_a_text_message(name: str) -> None:
    text = only_text(name)
    event = recorded_event(name)

    assert text.text == event["message"]["text"]
    assert text.message_id == event["message"]["mid"]
    assert text.sender_id == SENDER_ID
    assert text.sent_at == datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC)


def test_text_keeps_the_letters_the_user_typed() -> None:
    assert only_text(TEXT_QUESTION).text == "Restaurants in beşiktaş"


# --- Events that are not for us ----------------------------------------------


def test_dashboard_test_sample_holds_no_messages() -> None:
    assert parse_webhook(fixture_body(DASHBOARD_SAMPLE)) == []


def test_webhook_for_another_object_is_ignored() -> None:
    payload = fixture_json(REEL_SHARE)
    payload["object"] = "page"

    assert parse_webhook(body_of(payload)) == []


@pytest.mark.parametrize("flag", ["is_echo", "is_deleted"])
def test_echo_of_the_bots_own_message_and_deleted_message_are_ignored(flag: str) -> None:
    message = {**fixture_message(TEXT_QUESTION), flag: True}

    assert parse_webhook(webhook_with_message(message)) == []


@pytest.mark.parametrize(
    "event_fields",
    [
        {"read": {"mid": "aWdfZAGFAKEMID0005"}},
        {"reaction": {"mid": "aWdfZAGFAKEMID0005", "action": "react", "reaction": "love"}},
    ],
)
def test_event_without_a_message_is_ignored(event_fields: dict[str, Any]) -> None:
    event = recorded_event(TEXT_QUESTION)
    del event["message"]
    event.update(event_fields)

    assert parse_webhook(webhook_with_events(event)) == []


@pytest.mark.parametrize("attachment_type", ["image", "video", "audio", "story_mention"])
def test_attachment_that_is_not_a_share_is_ignored(attachment_type: str) -> None:
    message = fixture_message(PHOTO_POST_SHARE)
    message["attachments"][0]["type"] = attachment_type

    assert parse_webhook(webhook_with_message(message)) == []


def test_unsupported_type_without_a_link_is_ignored() -> None:
    message = fixture_message(COLLECTION_REEL)
    message["attachments"][0]["payload"] = {}

    assert parse_webhook(webhook_with_message(message)) == []


@pytest.mark.parametrize("text", ["", "   \n"])
def test_blank_text_is_ignored(text: str) -> None:
    message = {**fixture_message(TEXT_QUESTION), "text": text}

    assert parse_webhook(webhook_with_message(message)) == []


@pytest.mark.parametrize(
    "broken_fields",
    [
        {"timestamp": "1768478506899"},
        {"timestamp": True},
        {"timestamp": 10**30},
        {"sender": {}},
        {"message": {"text": "no message id"}},
    ],
)
def test_event_in_an_unknown_shape_is_skipped_and_the_others_are_kept(
    broken_fields: dict[str, Any],
) -> None:
    broken_event = {**recorded_event(TEXT_QUESTION), **broken_fields}
    good_event = recorded_event(REEL_SHARE)

    messages = parse_webhook(webhook_with_events(broken_event, good_event))

    assert [type(message) for message in messages] == [InstagramShare]


def test_messages_keep_the_order_meta_sent_them_in() -> None:
    body = webhook_with_events(recorded_event(REEL_SHARE), recorded_event(HINT_AFTER_REEL))

    assert [type(message) for message in parse_webhook(body)] == [InstagramShare, InstagramText]


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"text"', b'{"object": "instagram"}', b'{"entry": []}'],
)
def test_body_that_is_not_a_webhook_object_is_rejected(body: bytes) -> None:
    with pytest.raises(InvalidWebhookPayloadError):
        parse_webhook(body)


def test_rejected_body_is_not_repeated_in_the_error() -> None:
    private_text = "synthetic private words 81fa"
    body = body_of({"object": "instagram", "entry": private_text})

    with pytest.raises(InvalidWebhookPayloadError) as error:
        parse_webhook(body)

    assert private_text not in str(error.value)
    assert error.value.__cause__ is None


# --- Platform-independent objects ---------------------------------------------


def test_capture_event_of_a_reel_share_carries_the_message_id() -> None:
    share = only_share(REEL_SHARE)

    event = to_capture_event(share, USER_ID)

    assert event.user_id == USER_ID
    assert event.platform == "instagram"
    assert event.source == "dm"
    assert event.source_message_id == share.message_id
    assert event.platform_post_id == share.platform_post_id
    assert event.post_url == share.post_url
    assert event.media_url is None
    assert event.media_type == "reel"
    assert event.caption == share.caption
    assert event.collection_hint is None
    assert event.received_at == share.sent_at


def test_capture_event_of_a_collection_reel_has_no_post_link_and_no_post_id() -> None:
    share = only_share(COLLECTION_REEL)

    event = to_capture_event(share, USER_ID)

    assert event.post_url is None
    assert event.platform_post_id is None
    assert event.media_url == share.media_url
    assert event.source_message_id == share.message_id


def test_chat_message_carries_the_text_and_the_message_id() -> None:
    text = only_text(TEXT_QUESTION)

    message = to_chat_message(text, USER_ID)

    assert message.user_id == USER_ID
    assert message.platform == "instagram"
    assert message.text == "Restaurants in beşiktaş"
    assert message.message_id == text.message_id
    assert message.received_at == text.sent_at
