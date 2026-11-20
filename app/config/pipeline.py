"""Settings of the indexing pipeline and its worker."""

from functools import lru_cache
from typing import Annotated, Self

from pydantic import (
    Field,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.base import SECONDS_PER_HOUR, SECONDS_PER_MINUTE, NonEmptyText

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
        indexing_sweep_batch_size: The most stuck items one sweep repairs
            (`INDEXING_SWEEP_BATCH_SIZE`); the next sweep takes the rest. Items
            whose owner's row is locked are skipped and don't count.
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
