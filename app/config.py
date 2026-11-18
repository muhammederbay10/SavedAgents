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

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

# Instagram accepts a reply only this long after the user's last message
# (Instagram "Messaging API" page). A setting can shorten the window, never widen it.
MAX_INSTAGRAM_REPLY_WINDOW_HOURS = 24.0


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
            the bot may still reply (`INSTAGRAM_REPLY_WINDOW_HOURS`), at most
            `MAX_INSTAGRAM_REPLY_WINDOW_HOURS` (Instagram's 24 hours). A message
            for an older conversation is skipped. Every number here must be finite.
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
    instagram_reply_window_hours: float = Field(
        gt=0, le=MAX_INSTAGRAM_REPLY_WINDOW_HOURS, allow_inf_nan=False
    )
    send_api_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    send_api_max_attempts: int = Field(ge=1)
    send_api_retry_base_delay_seconds: float = Field(gt=0, allow_inf_nan=False)
    send_api_retry_max_delay_seconds: float = Field(gt=0, allow_inf_nan=False)

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


# A probability from a decision model, as a threshold.
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class PipelineSettings(BaseSettings):
    """Configuration of the indexing pipeline and its worker in `app/pipeline/`.

    It is separate from `Settings` so that the API, which indexes nothing, does
    not need it. Like `Settings`, values come from environment variables with
    `.env` as a fallback, no field has a default, and a validation error names
    the field but never repeats its value.

    Attributes:
        media_storage_dir: The folder that stands in for object storage in
            development (`MEDIA_STORAGE_DIR`). Each saved item's media goes into
            `<folder>/<user id>/<saved item id>/`, and "delete my data" removes
            the user's folder.
        media_download_timeout_seconds: Time limit of one media download
            (`MEDIA_DOWNLOAD_TIMEOUT_SECONDS`).
        media_max_download_bytes: The largest media file that is downloaded
            (`MEDIA_MAX_DOWNLOAD_BYTES`). A larger one is treated as unavailable.
        media_stored_height_pixels: The height of the stored video copy and
            images (`MEDIA_STORED_HEIGHT_PIXELS`). Smaller media is not enlarged.
        ffmpeg_path: The `ffmpeg` program (`FFMPEG_PATH`), a name on `PATH` or a path.
        ffprobe_path: The `ffprobe` program (`FFPROBE_PATH`).
        media_tool_timeout_seconds: Time limit of one ffmpeg or ffprobe run
            (`MEDIA_TOOL_TIMEOUT_SECONDS`).
        media_tool_stop_timeout_seconds: How long a stopped ffmpeg or ffprobe run
            (timed out, or cancelled by the run's deadline) waits for the program
            to exit, after SIGTERM and again after SIGKILL
            (`MEDIA_TOOL_STOP_TIMEOUT_SECONDS`).
        transcript_min_speech_characters: The near-empty rule
            (`TRANSCRIPT_MIN_SPEECH_CHARACTERS`). A final transcript with fewer
            letters and digits than this, in any script, counts as no speech.
            Punctuation, symbols and spaces are not counted, so the rule works
            the same in every language.
        classify_is_list_min_probability: A post counts as a list of several
            things from this probability of yes (`CLASSIFY_IS_LIST_MIN_PROBABILITY`).
        extraction_temperature: Sampling temperature of extraction and of the
            escalation (`EXTRACTION_TEMPERATURE`).
        extraction_max_output_tokens: Upper limit on the length of their replies
            (`EXTRACTION_MAX_OUTPUT_TOKENS`).
        vocab_privacy_min_probability: The privacy gate passes wording only when
            the decision model's "generic" probability is at least this
            (`VOCAB_PRIVACY_MIN_PROBABILITY`). Anything below counts as no.
        vocab_alias_min_probability: A match counts towards verifying its alias
            only with at least this probability (`VOCAB_ALIAS_MIN_PROBABILITY`).
        vocab_alias_min_users: A candidate alias becomes verified once this many
            different users had a confident match through it (`VOCAB_ALIAS_MIN_USERS`).
        vocab_max_value_length: Longer wording never enters the shared
            vocabulary (`VOCAB_MAX_VALUE_LENGTH`), in characters.
        vocab_phone_min_digits: Wording with a run of at least this many digits,
            allowing spaces, dots, dashes, slashes, brackets and a leading plus
            between them, looks like a phone number and never enters the shared
            vocabulary (`VOCAB_PHONE_MIN_DIGITS`).
        vocab_match_candidates: How many of the nearest existing values of the
            same field the decision model chooses from (`VOCAB_MATCH_CANDIDATES`).
        indexing_concurrency: How many items one worker indexes at the same
            time (`INDEXING_CONCURRENCY`). A bulk import's jobs fan out over this
            many slots per worker; more workers add more.
        indexing_max_tries: How often one item's indexing job runs before the
            item is marked failed, including the first run (`INDEXING_MAX_TRIES`).
        indexing_retry_delay_seconds: The wait before a failed job runs again.
            It grows with each try (`INDEXING_RETRY_DELAY_SECONDS`).
        indexing_run_deadline_seconds: How long one run may take, from before it
            reads the payload to the end of its last step
            (`INDEXING_RUN_DEADLINE_SECONDS`). A run that passes it stops itself,
            records where it was, and is tried again; the last try marks the
            item failed.
        indexing_failure_recording_seconds: How long recording a failure may
            take after the run stopped (`INDEXING_FAILURE_RECORDING_SECONDS`).
            If the database doesn't answer in time, the failure is logged and
            not recorded, and the job still ends itself.
        indexing_cleanup_reserve_seconds: Time kept free inside arq's limit for
            clean-up after a run is stopped (`INDEXING_CLEANUP_RESERVE_SECONDS`):
            stopping and reaping a media tool, rolling back a transaction. It
            must cover a media tool's stop (twice `MEDIA_TOOL_STOP_TIMEOUT_SECONDS`).
        indexing_job_timeout_seconds: arq's own limit on one run of one job
            (`INDEXING_JOB_TIMEOUT_SECONDS`), a backstop that cancels the job
            without a retry. It must be longer than the run deadline, the
            recording allowance and the clean-up reserve together.
        indexing_sweep_interval_seconds: How often the stuck-item sweeper runs
            (`INDEXING_SWEEP_INTERVAL_SECONDS`). It runs as an arq cron, so the
            interval must divide a minute, or be whole minutes that divide an hour.
        indexing_stale_after_seconds: An unfinished item counts as stuck when
            no job or sweep has worked on it for this long
            (`INDEXING_STALE_AFTER_SECONDS`). It must be longer than the longest
            a live job can stay silent: a whole run plus every retry wait.
        indexing_sweep_batch_size: The most stuck items one sweep handles
            (`INDEXING_SWEEP_BATCH_SIZE`); the next sweep takes the rest.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    media_storage_dir: NonEmptyText
    media_download_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    media_max_download_bytes: int = Field(gt=0)
    media_stored_height_pixels: int = Field(gt=0)
    ffmpeg_path: NonEmptyText
    ffprobe_path: NonEmptyText
    media_tool_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    media_tool_stop_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)

    transcript_min_speech_characters: int = Field(ge=1)

    classify_is_list_min_probability: Probability

    extraction_temperature: float = Field(ge=0, allow_inf_nan=False)
    extraction_max_output_tokens: int = Field(gt=0)

    vocab_privacy_min_probability: Probability
    vocab_alias_min_probability: Probability
    vocab_alias_min_users: int = Field(ge=1)
    vocab_max_value_length: int = Field(gt=0)
    vocab_phone_min_digits: int = Field(ge=1)
    vocab_match_candidates: int = Field(ge=1)

    indexing_concurrency: int = Field(ge=1)
    indexing_max_tries: int = Field(ge=1)
    indexing_retry_delay_seconds: float = Field(gt=0, allow_inf_nan=False)
    indexing_run_deadline_seconds: float = Field(gt=0, allow_inf_nan=False)
    indexing_failure_recording_seconds: float = Field(gt=0, allow_inf_nan=False)
    indexing_cleanup_reserve_seconds: float = Field(gt=0, allow_inf_nan=False)
    indexing_job_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    indexing_sweep_interval_seconds: int = Field(gt=0)
    indexing_stale_after_seconds: float = Field(gt=0, allow_inf_nan=False)
    indexing_sweep_batch_size: int = Field(gt=0)

    @model_validator(mode="after")
    def _the_run_ends_itself_before_arq_does(self) -> Self:
        own_budget = (
            self.indexing_run_deadline_seconds
            + self.indexing_failure_recording_seconds
            + self.indexing_cleanup_reserve_seconds
        )
        if self.indexing_job_timeout_seconds <= own_budget:
            raise ValueError(
                "indexing_job_timeout_seconds must be longer than indexing_run_deadline_seconds "
                "plus indexing_failure_recording_seconds plus indexing_cleanup_reserve_seconds"
            )
        if self.indexing_cleanup_reserve_seconds < 2 * self.media_tool_stop_timeout_seconds:
            raise ValueError(
                "indexing_cleanup_reserve_seconds must be at least twice "
                "media_tool_stop_timeout_seconds"
            )
        return self

    @model_validator(mode="after")
    def _the_sweeper_never_touches_a_live_job(self) -> Self:
        longest_silence = (
            self.indexing_job_timeout_seconds
            + self.indexing_retry_delay_seconds * self.indexing_max_tries
        )
        if self.indexing_stale_after_seconds <= longest_silence:
            raise ValueError(
                "indexing_stale_after_seconds must be longer than indexing_job_timeout_seconds "
                "plus indexing_retry_delay_seconds times indexing_max_tries"
            )
        return self

    @model_validator(mode="after")
    def _the_sweep_interval_fits_a_cron(self) -> Self:
        interval = self.indexing_sweep_interval_seconds
        fits_a_minute = interval < SECONDS_PER_MINUTE and SECONDS_PER_MINUTE % interval == 0
        fits_an_hour = interval % SECONDS_PER_MINUTE == 0 and SECONDS_PER_HOUR % interval == 0
        if not (fits_a_minute or fits_an_hour):
            raise ValueError(
                "indexing_sweep_interval_seconds must divide a minute, "
                "or be whole minutes that divide an hour"
            )
        return self


@lru_cache
def get_pipeline_settings() -> PipelineSettings:
    """Return the process-wide pipeline settings, read once from the environment.

    Returns:
        The cached `PipelineSettings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return PipelineSettings()
