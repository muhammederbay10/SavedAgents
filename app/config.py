"""Application settings, loaded from environment variables.

Every tunable value (URLs, limits, thresholds, model names) belongs here, so the
rest of the code never hard-codes configuration.
"""

from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    HttpUrl,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    StringConstraints,
    UrlConstraints,
    model_validator,
)
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
            `saved_item.embedding` and `vocabulary.embedding`
            (`EMBEDDING_DIMENSION`). The first migration creates both columns
            with this size, so it must match the embedding model.
        meta_app_secret: The Meta app's secret (`META_APP_SECRET`). Every
            webhook body is signed with it (`X-Hub-Signature-256`).
        meta_webhook_verify_token: The token Meta repeats in the webhook
            verification handshake (`META_WEBHOOK_VERIFY_TOKEN`). We choose it
            and enter the same value in the Meta app dashboard.
        collection_hint_window_seconds: How long after a share a text from the
            same user still counts as that item's collection hint
            (`COLLECTION_HINT_WINDOW_SECONDS`).
        ingestion_state_ttl_seconds: How long ingestion keeps a user's job
            payloads, text receipts and recent shares in Redis after its last
            write (`INGESTION_STATE_TTL_SECONDS`). It must cover the time Meta
            keeps delivering a failed webhook again, so a late delivery is
            still recognized, and it must be at least the hint window.
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
    meta_app_secret: SecretStr = Field(min_length=1)
    meta_webhook_verify_token: SecretStr = Field(min_length=1)
    collection_hint_window_seconds: int = Field(gt=0)
    ingestion_state_ttl_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _state_outlives_the_hint_window(self) -> Self:
        if self.ingestion_state_ttl_seconds < self.collection_hint_window_seconds:
            raise ValueError(
                "ingestion_state_ttl_seconds must be at least collection_hint_window_seconds"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment.

    Returns:
        The cached `Settings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return Settings()


# `verbose_json` adds the detected language and timestamps; some speech-to-text
# models reject it and only accept `json`.
TranscriptionResponseFormat = Literal["json", "verbose_json"]

# The API key travels in a header of every request, so plain HTTP is refused.
HttpsUrl = Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https"])]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelSettings(BaseSettings):
    """Configuration of the model clients in `app/models/`.

    It is separate from `Settings` so that the parts of the system that call no
    model (the health endpoint, the migrations) do not need an API key. Like
    `Settings`, values come from environment variables with `.env` as a
    fallback, no field has a default, and a validation error names the field
    but never repeats its value.

    Every model call goes through OpenRouter (decision record 0008). The model
    ids are settings, so a model is swapped without a code change.

    Attributes:
        openrouter_api_key: The OpenRouter key (`OPENROUTER_API_KEY`).
        openrouter_base_url: Base URL of the OpenAI-compatible API, used for
            chat completions, transcriptions and embeddings (`OPENROUTER_BASE_URL`).
        openrouter_decisions_url: Full URL of the decisions endpoint, which is
            not under the base URL and is not chat completions
            (`OPENROUTER_DECISIONS_URL`).
        openrouter_max_attempts: How often one call is tried before it fails,
            including the first try (`OPENROUTER_MAX_ATTEMPTS`).
        openrouter_retry_base_delay_seconds: The wait before the second try. It
            doubles before each later one (`OPENROUTER_RETRY_BASE_DELAY_SECONDS`).
        openrouter_retry_max_delay_seconds: The longest wait before any try
            (`OPENROUTER_RETRY_MAX_DELAY_SECONDS`).
        decision_model: Model for fixed decisions (`DECISION_MODEL`).
        decision_timeout_seconds: Time limit of one decision request
            (`DECISION_TIMEOUT_SECONDS`).
        text_model: Model for extraction and the response agent (`TEXT_MODEL`).
        text_model_provider: The one provider endpoint that may serve the text
            model, as an OpenRouter slug (`TEXT_MODEL_PROVIDER`). It must be
            hosted outside China. Requests never fall back to another provider.
        text_model_provider_name: The name OpenRouter reports for that provider
            in a response's `provider` field (`TEXT_MODEL_PROVIDER_NAME`). A
            reply that names another provider is discarded.
        text_timeout_seconds: Time limit of one text request (`TEXT_TIMEOUT_SECONDS`).
        escalation_model: Video model for the rare escalation (`ESCALATION_MODEL`).
        escalation_timeout_seconds: Time limit of one escalation request
            (`ESCALATION_TIMEOUT_SECONDS`).
        transcription_model: Default speech-to-text model (`TRANSCRIPTION_MODEL`).
        transcription_response_format: Response format the default model is
            asked for (`TRANSCRIPTION_RESPONSE_FORMAT`).
        transcription_fallback_model: Speech-to-text model used when the default
            fails or returns no words (`TRANSCRIPTION_FALLBACK_MODEL`).
        transcription_fallback_response_format: Response format the fallback
            model is asked for (`TRANSCRIPTION_FALLBACK_RESPONSE_FORMAT`).
        transcription_timeout_seconds: Time limit of one transcription request
            (`TRANSCRIPTION_TIMEOUT_SECONDS`).
        embedding_model: Embedding model (`EMBEDDING_MODEL`).
        embedding_dimension: Vector size requested from the embedding model and
            required in its answer (`EMBEDDING_DIMENSION`, the same variable
            `Settings` reads for the database columns).
        embedding_timeout_seconds: Time limit of one embedding request
            (`EMBEDDING_TIMEOUT_SECONDS`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    openrouter_api_key: SecretStr = Field(min_length=1)
    openrouter_base_url: HttpsUrl
    openrouter_decisions_url: HttpsUrl
    openrouter_max_attempts: int = Field(ge=1)
    openrouter_retry_base_delay_seconds: float = Field(gt=0)
    openrouter_retry_max_delay_seconds: float = Field(gt=0)

    decision_model: NonEmptyText
    decision_timeout_seconds: float = Field(gt=0)

    text_model: NonEmptyText
    text_model_provider: NonEmptyText
    text_model_provider_name: NonEmptyText
    text_timeout_seconds: float = Field(gt=0)

    escalation_model: NonEmptyText
    escalation_timeout_seconds: float = Field(gt=0)

    transcription_model: NonEmptyText
    transcription_response_format: TranscriptionResponseFormat
    transcription_fallback_model: NonEmptyText
    transcription_fallback_response_format: TranscriptionResponseFormat
    transcription_timeout_seconds: float = Field(gt=0)

    embedding_model: NonEmptyText
    embedding_dimension: int = Field(gt=0, le=MAX_EMBEDDING_DIMENSION)
    embedding_timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _longest_wait_is_not_below_the_first(self) -> Self:
        if self.openrouter_retry_max_delay_seconds < self.openrouter_retry_base_delay_seconds:
            raise ValueError(
                "openrouter_retry_max_delay_seconds must not be below "
                "openrouter_retry_base_delay_seconds"
            )
        return self


@lru_cache
def get_model_settings() -> ModelSettings:
    """Return the process-wide model settings, read once from the environment.

    Returns:
        The cached `ModelSettings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return ModelSettings()


class MessengerSettings(BaseSettings):
    """Configuration of the Instagram Send API client in `app/agent/messenger.py`.

    It is separate from `Settings` so that the parts of the system that send no
    DM (the health endpoint, the migrations) do not need the bot's access
    token. Like `Settings`, values come from environment variables with `.env`
    as a fallback, no field has a default, and a validation error names the
    field but never repeats its value.

    Attributes:
        instagram_graph_api_url: Base URL of the Graph API, with its version,
            e.g. `https://graph.facebook.com/v24.0` (`INSTAGRAM_GRAPH_API_URL`).
            Messages are posted to `<this URL>/<account id>/messages`.
        instagram_account_id: The bot's Instagram professional account id, the
            `{ig-user-id}` of the Send API (`INSTAGRAM_ACCOUNT_ID`).
        instagram_access_token: The bot's access token for the Send API
            (`INSTAGRAM_ACCESS_TOKEN`). It is sent in a header, never in a URL.
        instagram_reply_window_hours: How long after the user's last message
            the bot may still reply (`INSTAGRAM_REPLY_WINDOW_HOURS`). Instagram
            allows 24 hours; a message for an older conversation is skipped.
        send_api_timeout_seconds: Time limit of one Send API request
            (`SEND_API_TIMEOUT_SECONDS`).
        send_api_max_attempts: How often one message is tried before it fails,
            including the first try (`SEND_API_MAX_ATTEMPTS`).
        send_api_retry_base_delay_seconds: The wait before the second try. It
            doubles before each later one (`SEND_API_RETRY_BASE_DELAY_SECONDS`).
        send_api_retry_max_delay_seconds: The longest wait before any try, also
            when Meta asks for a longer one (`SEND_API_RETRY_MAX_DELAY_SECONDS`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # The access token travels in a header of every request, so plain HTTP is refused.
    instagram_graph_api_url: Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https"])]
    instagram_account_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    instagram_access_token: SecretStr = Field(min_length=1)
    instagram_reply_window_hours: float = Field(gt=0)
    send_api_timeout_seconds: float = Field(gt=0)
    send_api_max_attempts: int = Field(ge=1)
    send_api_retry_base_delay_seconds: float = Field(gt=0)
    send_api_retry_max_delay_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _longest_wait_is_not_below_the_first(self) -> Self:
        if self.send_api_retry_max_delay_seconds < self.send_api_retry_base_delay_seconds:
            raise ValueError(
                "send_api_retry_max_delay_seconds must not be below "
                "send_api_retry_base_delay_seconds"
            )
        return self


@lru_cache
def get_messenger_settings() -> MessengerSettings:
    """Return the process-wide Send API settings, read once from the environment.

    Returns:
        The cached `MessengerSettings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return MessengerSettings()
