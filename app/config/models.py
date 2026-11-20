"""Settings of the model clients in `app/models/`."""

from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    UrlConstraints,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.base import MAX_EMBEDDING_DIMENSION, NonEmptyText

# `verbose_json` adds the detected language and timestamps; some speech-to-text
# models reject it and only accept `json`.
TranscriptionResponseFormat = Literal["json", "verbose_json"]

# The API key travels in a header of every request, so plain HTTP is refused.
HttpsUrl = Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https"])]


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
