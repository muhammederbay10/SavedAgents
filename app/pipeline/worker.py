"""The indexing worker: runs `index_saved_item` jobs from the arq queue.

Run it with `python -m app.pipeline.worker`. It reads `Settings`,
`ModelSettings`, `PipelineSettings` and `MessengerSettings` from the
environment, and needs `ffmpeg` and `ffprobe` on the machine.

**Parallel fan-out.** One job indexes one saved item (decision record 0009).
A worker runs up to `INDEXING_CONCURRENCY` jobs at once, so the jobs of a bulk
import spread over every free slot of every running worker; more workers add
more slots. The providers' rate limits set the real ceiling.

**Retries.** A step that fails in a way that may pass later raises
`RetryLaterError`; the job is tried again after a growing wait, up to
`INDEXING_MAX_TRIES` runs. The last run marks the item `failed`. Each run
continues after the steps that already finished (`app.pipeline.indexer`).

The job's arguments are ids, and its result is the outcome's name, so neither
holds a caption, a link or any other content.
"""

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
from arq import Retry
from arq.connections import RedisSettings
from arq.worker import Worker, func
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.messenger import InstagramMessenger
from app.config import (
    get_messenger_settings,
    get_model_settings,
    get_pipeline_settings,
    get_settings,
)
from app.db.session import create_engine, create_session_factory
from app.ingestion.jobs import INDEX_SAVED_ITEM_JOB
from app.logging_config import configure_logging
from app.models.factory import build_model_clients, open_http_client
from app.pipeline.classify import PostClassifier
from app.pipeline.confirm import SaveConfirmer
from app.pipeline.embed import ItemEmbedder
from app.pipeline.extract import Extractor
from app.pipeline.fetch import MediaFetcher
from app.pipeline.indexer import ItemIndexer, PipelineSteps, RetryLaterError
from app.pipeline.media import FfmpegMediaTools
from app.pipeline.normalize import Normalizer, VocabularyRules
from app.pipeline.payloads import CapturePayloads
from app.pipeline.prompts import PIPELINE_PROMPTS
from app.pipeline.storage import LocalMediaStore
from app.pipeline.transcribe import SpeechToText
from app.types.registry import CONTENT_TYPES

# Where the worker keeps what its jobs share, in arq's context.
_RESOURCES: Final = "indexing_resources"
# arq's context key for the number of the current run of a job, from 1.
_JOB_TRY: Final = "job_try"
# arq's context key for its own Redis client, which also holds ingestion's state.
_REDIS: Final = "redis"

# arq hands every job a context dictionary of its own making; its values are
# of any type, so it cannot be typed more narrowly.
JobContext = dict[str, Any]


@dataclass(frozen=True)
class WorkerResources:
    """What every job of one worker shares, and what is closed when it stops.

    Attributes:
        indexer: Indexes one item.
        max_tries: `INDEXING_MAX_TRIES`.
        retry_delay_seconds: `INDEXING_RETRY_DELAY_SECONDS`.
        engine: The database engine.
        http_clients: The HTTP clients for the models, media downloads and DMs.
    """

    indexer: ItemIndexer
    max_tries: int
    retry_delay_seconds: float
    engine: AsyncEngine
    http_clients: tuple[httpx.AsyncClient, ...]


async def index_saved_item(ctx: JobContext, *, user_id: str, saved_item_id: str) -> str:
    """Index one saved item; the arq job queued by ingestion.

    Args:
        ctx: arq's job context.
        user_id: The item's owner, as text.
        saved_item_id: The item, as text.

    Returns:
        The outcome's name, e.g. `indexed`.

    Raises:
        arq.Retry: If a step failed in a way that may pass later, and this was
            not the last try.
    """
    resources: WorkerResources = ctx[_RESOURCES]
    job_try: int = ctx[_JOB_TRY]
    try:
        outcome = await resources.indexer.index(
            uuid.UUID(user_id),
            uuid.UUID(saved_item_id),
            last_try=job_try >= resources.max_tries,
        )
    except RetryLaterError as error:
        raise Retry(defer=resources.retry_delay_seconds * job_try) from error
    return outcome.value


async def start_worker(ctx: JobContext) -> None:
    """Build the indexer and everything it needs, once per worker.

    Args:
        ctx: arq's worker context. Its Redis client also reads ingestion's payloads.

    Raises:
        pydantic.ValidationError: If a required setting is missing or invalid.
    """
    settings = get_settings()
    pipeline_settings = get_pipeline_settings()
    model_settings = get_model_settings()
    configure_logging(settings.log_level)
    engine = create_engine(str(settings.database_url))
    sessions = create_session_factory(engine)
    model_http = open_http_client(model_settings)
    # Media links carry their own signature; nothing else is sent with them.
    media_http = httpx.AsyncClient()
    messenger_http = httpx.AsyncClient()
    models = build_model_clients(model_settings, model_http)
    store = LocalMediaStore(Path(pipeline_settings.media_storage_dir))
    tools = FfmpegMediaTools(
        ffmpeg_path=pipeline_settings.ffmpeg_path,
        ffprobe_path=pipeline_settings.ffprobe_path,
        timeout_seconds=pipeline_settings.media_tool_timeout_seconds,
    )
    rules = VocabularyRules(
        privacy_min_probability=pipeline_settings.vocab_privacy_min_probability,
        alias_min_probability=pipeline_settings.vocab_alias_min_probability,
        alias_min_users=pipeline_settings.vocab_alias_min_users,
        max_value_length=pipeline_settings.vocab_max_value_length,
        phone_min_digits=pipeline_settings.vocab_phone_min_digits,
        match_candidates=pipeline_settings.vocab_match_candidates,
    )
    steps = PipelineSteps(
        fetcher=MediaFetcher(
            media_http,
            store,
            tools,
            timeout_seconds=pipeline_settings.media_download_timeout_seconds,
            max_bytes=pipeline_settings.media_max_download_bytes,
            stored_height_pixels=pipeline_settings.media_stored_height_pixels,
        ),
        classifier=PostClassifier(
            models.classifier,
            CONTENT_TYPES,
            PIPELINE_PROMPTS,
            is_list_min_probability=pipeline_settings.classify_is_list_min_probability,
        ),
        speech=SpeechToText(
            models.transcriber,
            tools,
            store,
            min_speech_characters=pipeline_settings.transcript_min_speech_characters,
        ),
        extractor=Extractor(
            models.text_generator,
            models.escalation_generator,
            CONTENT_TYPES,
            PIPELINE_PROMPTS,
            store,
            temperature=pipeline_settings.extraction_temperature,
            max_output_tokens=pipeline_settings.extraction_max_output_tokens,
        ),
        normalizer=Normalizer(
            sessions, models.classifier, models.embedder, PIPELINE_PROMPTS, rules
        ),
        embedder=ItemEmbedder(models.embedder),
        # Instagram is the only platform with a messenger so far (ARCHITECTURE §3.1).
        confirmer=SaveConfirmer(InstagramMessenger(messenger_http, get_messenger_settings())),
        store=store,
    )
    ctx[_RESOURCES] = WorkerResources(
        indexer=ItemIndexer(
            sessions,
            CapturePayloads(ctx[_REDIS]),
            steps,
            CONTENT_TYPES,
            PIPELINE_PROMPTS,
            alias_min_probability=rules.alias_min_probability,
        ),
        max_tries=pipeline_settings.indexing_max_tries,
        retry_delay_seconds=pipeline_settings.indexing_retry_delay_seconds,
        engine=engine,
        http_clients=(model_http, media_http, messenger_http),
    )


async def stop_worker(ctx: JobContext) -> None:
    """Close the worker's HTTP clients and database engine.

    Args:
        ctx: arq's worker context.
    """
    resources: WorkerResources | None = ctx.get(_RESOURCES)
    if resources is None:
        return
    await asyncio.gather(*(client.aclose() for client in resources.http_clients))
    await resources.engine.dispose()


def build_worker() -> Worker:
    """Create the arq worker for indexing jobs, from the settings.

    Returns:
        The worker. `run()` starts it.

    Raises:
        pydantic.ValidationError: If a required setting is missing or invalid.
    """
    settings = get_settings()
    pipeline_settings = get_pipeline_settings()
    return Worker(
        functions=[
            func(
                index_saved_item,
                name=INDEX_SAVED_ITEM_JOB,
                max_tries=pipeline_settings.indexing_max_tries,
            )
        ],
        redis_settings=RedisSettings.from_dsn(str(settings.redis_url)),
        on_startup=start_worker,
        on_shutdown=stop_worker,
        max_jobs=pipeline_settings.indexing_concurrency,
        job_timeout=pipeline_settings.indexing_job_timeout_seconds,
        max_tries=pipeline_settings.indexing_max_tries,
    )


if __name__ == "__main__":
    build_worker().run()
