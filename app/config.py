"""Application settings, loaded from environment variables.

Every tunable value (URLs, limits, thresholds, model names) belongs here, so the
rest of the code never hard-codes configuration.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# pgvector cannot build an HNSW index on a `vector` column with more dimensions.
MAX_EMBEDDING_DIMENSION = 2000


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
        embedding_dimension: Length of the embedding vectors stored in
            `saved_item.embedding` (`EMBEDDING_DIMENSION`). The first migration
            creates the column with this size, so it must match the embedding
            model.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    log_level: LogLevel
    database_url: PostgresDsn
    redis_url: RedisDsn
    embedding_dimension: int = Field(gt=0, le=MAX_EMBEDDING_DIMENSION)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment.

    Returns:
        The cached `Settings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return Settings()
