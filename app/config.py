"""Application settings, loaded from environment variables.

Every tunable value (URLs, limits, thresholds, model names) belongs here, so the
rest of the code never hard-codes configuration.
"""

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration for the API.

    Values come from environment variables. A local `.env` file is read as a
    fallback, and real environment variables take priority over it. No field
    has a default: a missing variable fails at startup instead of running with
    a guessed value.

    Validation errors name the field that is wrong but never repeat its value,
    because a rejected URL can contain a password.

    Attributes:
        log_level: Minimum severity that is written to the log (`LOG_LEVEL`).
        database_url: Postgres connection URL (`DATABASE_URL`).
        redis_url: Redis connection URL (`REDIS_URL`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    log_level: LogLevel
    database_url: PostgresDsn
    redis_url: RedisDsn


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment.

    Returns:
        The cached `Settings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return Settings()
