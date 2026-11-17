"""FastAPI application: app factory, the health endpoint and the webhook routes.

Run with `uvicorn app.main:create_app --factory`.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.webhooks import create_instagram_webhook_router
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.ingestion.jobs import create_arq_job_queue
from app.ingestion.service import IngestionService
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """Body returned by `GET /health`."""

    status: Literal["ok"]


async def health() -> HealthResponse:
    """Report that the API process is up and able to serve requests.

    This is a liveness check only. It does not contact Postgres or Redis.

    Returns:
        A response with `status` set to `"ok"`.
    """
    return HealthResponse(status="ok")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Building it opens no connection. Postgres and Redis are first contacted by
    the first webhook, and their connections are closed when the app shuts down.

    Args:
        settings: Configuration to use. Defaults to the settings read from the
            environment.

    Returns:
        The configured application.

    Raises:
        pydantic.ValidationError: If no settings are given and a required
            environment variable is missing or invalid.
    """
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    engine = create_engine(str(app_settings.database_url))
    job_queue = create_arq_job_queue(str(app_settings.redis_url))
    ingestion = IngestionService(
        session_factory=create_session_factory(engine),
        job_queue=job_queue,
        collection_hint_window=timedelta(seconds=app_settings.collection_hint_window_seconds),
    )

    @asynccontextmanager
    async def close_connections_on_shutdown(_: FastAPI) -> AsyncIterator[None]:
        yield
        await job_queue.close()
        await engine.dispose()

    app = FastAPI(title="SavedAgent", lifespan=close_connections_on_shutdown)
    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(create_instagram_webhook_router(app_settings, ingestion))

    logger.info("app_created", extra={"log_level": app_settings.log_level})
    return app
