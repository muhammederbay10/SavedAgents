"""FastAPI application: app factory and the health endpoint.

Run with `uvicorn app.main:create_app --factory`.
"""

import logging
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import Settings, get_settings
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

    app = FastAPI(title="SavedAgent")
    app.add_api_route("/health", health, methods=["GET"])

    logger.info("app_created", extra={"log_level": app_settings.log_level})
    return app
