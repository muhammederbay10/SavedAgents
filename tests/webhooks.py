"""Helpers for the webhook tests: fixture bodies, signatures and an in-memory job queue.

The bodies come from `tests/fixtures/webhooks/`. They are signed here with the
synthetic app secret from `tests/conftest.py`, the way Meta signs a real body.
"""

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.ingestion.session_state import (
    capture_payload_field,
    chat_payload_field,
    payloads_key,
)
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


async def capture_payload(redis: Redis, user_id: UUID, saved_item_id: UUID) -> dict[str, Any]:
    """Read the capture event an indexing job works on, as a worker would."""
    return await _payload(redis, user_id, capture_payload_field(saved_item_id))


async def chat_payload(redis: Redis, user_id: UUID, receipt_id: UUID) -> dict[str, Any]:
    """Read the chat message a chat job answers, as a worker would."""
    return await _payload(redis, user_id, chat_payload_field(receipt_id))


async def _payload(redis: Redis, user_id: UUID, field: str) -> dict[str, Any]:
    value = await redis.hget(payloads_key(user_id), field)  # type: ignore[misc]
    assert value is not None, f"no payload {field}"
    payload: dict[str, Any] = json.loads(value)
    return payload


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

    async def discard(self, job_ids: Sequence[str]) -> None:
        self.jobs = [job for job in self.jobs if job.job_id not in job_ids]

    def named(self, job_name: str) -> list[QueuedJob]:
        return [job for job in self.jobs if job.name == job_name]
