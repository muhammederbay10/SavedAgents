"""Guards for the recorded Instagram webhook fixtures in tests/fixtures/webhooks/."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "webhooks"
REAL_DM_FIXTURES = sorted(FIXTURE_DIR.glob("instagram_*.json"))

FAKE_BOT_ID = "17840000000000001"
FAKE_SENDER_ID = "1000000000000001"
FAKE_MEDIA_ID = re.compile(r"^18000000000000\d{3}$")
FAKE_MID = re.compile(r"^aWdfZAGFAKEMID\d{4}A*$")
FAKE_REEL_URL = re.compile(r"^https://www\.instagram\.com/reel/FAKEREEL\d{3}/$")
FAKE_POST_URL = re.compile(
    r"^https://lookaside\.fbsbx\.com/ig_messaging_cdn/"
    r"\?asset_id=(18000000000000\d{3})&signature=FAKE_SIGNATURE_\d{3}$"
)


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def messaging_events(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for entry in data["entry"]:
        yield from entry["messaging"]


def test_real_dm_fixtures_exist() -> None:
    assert len(REAL_DM_FIXTURES) >= 5


@pytest.mark.parametrize("path", REAL_DM_FIXTURES, ids=lambda path: path.name)
def test_fixture_ids_are_fake(path: Path) -> None:
    data = load(path)
    assert data["object"] == "instagram"
    for entry in data["entry"]:
        assert entry["id"] == FAKE_BOT_ID
    for event in messaging_events(data):
        assert event["sender"]["id"] == FAKE_SENDER_ID
        assert event["recipient"]["id"] == FAKE_BOT_ID
        assert FAKE_MID.match(event["message"]["mid"])


@pytest.mark.parametrize("path", REAL_DM_FIXTURES, ids=lambda path: path.name)
def test_fixture_media_links_are_fake(path: Path) -> None:
    for event in messaging_events(load(path)):
        for attachment in event["message"].get("attachments", []):
            payload = attachment["payload"]
            if attachment["type"] == "ig_reel":
                assert FAKE_MEDIA_ID.match(payload["reel_video_id"])
                assert FAKE_REEL_URL.match(payload["url"])
            else:
                assert attachment["type"] == "ig_post"
                link = FAKE_POST_URL.match(payload["url"])
                assert link is not None
                assert link.group(1) == payload["ig_post_media_id"]


def test_dashboard_sample_uses_changes_not_messaging() -> None:
    sample = load(FIXTURE_DIR / "meta_dashboard_test_messages.json")
    entry = sample["entry"][0]
    assert "messaging" not in entry
    assert entry["changes"][0]["field"] == "messages"
