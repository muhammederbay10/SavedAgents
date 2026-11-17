"""Helpers for the webhook tests: fixture bodies, signatures and an in-memory job queue.

The bodies come from `tests/fixtures/webhooks/`. They are signed here with the
synthetic app secret from `tests/conftest.py`, the way Meta signs a real body.
"""

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.conftest import TEST_META_APP_SECRET

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "webhooks"
SIGNATURE_HEADER = "X-Hub-Signature-256"

REEL_SHARE = "instagram_reel_share.json"
PHOTO_POST_SHARE = "instagram_post_share_photo.json"
CAROUSEL_POST_SHARE = "instagram_post_share_carousel.json"
REEL_BEFORE_HINT = "instagram_reel_with_hint_1_reel.json"
HINT_AFTER_REEL = "instagram_reel_with_hint_2_text.json"
TEXT_QUESTION = "instagram_text_question.json"
TEXT_SHORT = "instagram_text_short.json"
COLLECTION_REEL = "instagram_collection_save_reel_1.json"
OTHER_COLLECTION_REEL = "instagram_collection_save_reel_2.json"
COLLECTION_REEL_WITH_CAPTION = "instagram_collection_save_reel_with_caption.json"
COLLECTION_PHOTO = "instagram_collection_save_photo.json"
# Built from Meta's documented shape: no `share` attachment was ever recorded.
LEGACY_SHARE = "synthetic_instagram_share_legacy.json"
DASHBOARD_SAMPLE = "meta_dashboard_test_messages.json"

SHARE_FIXTURES = (
    REEL_SHARE,
    PHOTO_POST_SHARE,
    CAROUSEL_POST_SHARE,
    REEL_BEFORE_HINT,
    COLLECTION_REEL,
    OTHER_COLLECTION_REEL,
    COLLECTION_REEL_WITH_CAPTION,
    COLLECTION_PHOTO,
    LEGACY_SHARE,
)
TEXT_FIXTURES = (TEXT_QUESTION, TEXT_SHORT, HINT_AFTER_REEL)


def fixture_body(name: str) -> bytes:
    """Return a fixture's bytes exactly as they are on disk."""
    return (FIXTURE_DIR / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(fixture_body(name))
    return data


def fixture_message(name: str) -> dict[str, Any]:
    """Return the `message` object of a fixture's only messaging event."""
    message: dict[str, Any] = fixture_json(name)["entry"][0]["messaging"][0]["message"]
    return message


def body_of(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


def signature_for(body: bytes, app_secret: str = TEST_META_APP_SECRET) -> str:
    """Sign a body the way Meta does: `sha256=` and the HMAC-SHA256 of the raw bytes."""
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def signed_headers(body: bytes) -> dict[str, str]:
    return {SIGNATURE_HEADER: signature_for(body), "Content-Type": "application/json"}


@dataclass(frozen=True)
class QueuedJob:
    name: str
    job_id: str
    arguments: Mapping[str, Any]


@dataclass
class RecordingJobQueue:
    """Keeps queued jobs in memory and refuses a job id it already holds, as arq does."""

    jobs: list[QueuedJob] = field(default_factory=list)

    async def enqueue(self, job_name: str, job_id: str, arguments: Mapping[str, Any]) -> bool:
        if any(job.job_id == job_id for job in self.jobs):
            return False
        self.jobs.append(QueuedJob(name=job_name, job_id=job_id, arguments=arguments))
        return True

    def named(self, job_name: str) -> list[QueuedJob]:
        return [job for job in self.jobs if job.name == job_name]
