from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.pipeline import PipelineSettings

REQUIRED_VARIABLES = {
    "MEDIA_STORAGE_DIR": "./media",
    "MEDIA_DOWNLOAD_TIMEOUT_SECONDS": "30",
    "MEDIA_MAX_DOWNLOAD_BYTES": "104857600",
    "MEDIA_STORED_HEIGHT_PIXELS": "360",
    "FFMPEG_PATH": "ffmpeg",
    "FFPROBE_PATH": "ffprobe",
    "MEDIA_TOOL_TIMEOUT_SECONDS": "120",
    "MEDIA_TOOL_STOP_TIMEOUT_SECONDS": "5",
    "TRANSCRIPT_MIN_SPEECH_CHARACTERS": "4",
    "CLASSIFY_IS_LIST_MIN_PROBABILITY": "0.5",
    "EXTRACTION_TEMPERATURE": "0",
    "EXTRACTION_MAX_OUTPUT_TOKENS": "2000",
    "VOCAB_PRIVACY_MIN_PROBABILITY": "0.9",
    "VOCAB_ALIAS_MIN_PROBABILITY": "0.9",
    "VOCAB_ALIAS_MIN_USERS": "3",
    "VOCAB_MAX_VALUE_LENGTH": "40",
    "VOCAB_PHONE_MIN_DIGITS": "7",
    "VOCAB_MATCH_CANDIDATES": "5",
    "INDEXING_CONCURRENCY": "10",
    "INDEXING_MAX_TRIES": "5",
    "INDEXING_RETRY_DELAY_SECONDS": "30",
    "INDEXING_RUN_DEADLINE_SECONDS": "540",
    "INDEXING_FAILURE_RECORDING_SECONDS": "20",
    "INDEXING_CLEANUP_RESERVE_SECONDS": "30",
    "INDEXING_SWEEP_INTERVAL_SECONDS": "300",
    "INDEXING_STALE_AFTER_SECONDS": "1800",
    "INDEXING_SWEEP_BATCH_SIZE": "100",
    "INDEXING_JOB_TIMEOUT_SECONDS": "600",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in REQUIRED_VARIABLES.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def load() -> PipelineSettings:
    return PipelineSettings(_env_file=None)


def test_pipeline_settings_are_read_from_environment_variables(
    environment: pytest.MonkeyPatch,
) -> None:
    settings = load()

    assert settings.transcript_min_speech_characters == 4
    assert settings.vocab_privacy_min_probability == 0.9
    assert settings.indexing_concurrency == 10


@pytest.mark.parametrize("missing_variable", sorted(REQUIRED_VARIABLES))
def test_every_pipeline_setting_is_required(
    environment: pytest.MonkeyPatch, missing_variable: str
) -> None:
    environment.delenv(missing_variable)

    with pytest.raises(ValidationError, match=missing_variable.lower()):
        load()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("VOCAB_PRIVACY_MIN_PROBABILITY", "1.5"),
        ("VOCAB_ALIAS_MIN_PROBABILITY", "-0.1"),
        ("CLASSIFY_IS_LIST_MIN_PROBABILITY", "nan"),
        ("TRANSCRIPT_MIN_SPEECH_CHARACTERS", "0"),
        ("INDEXING_MAX_TRIES", "0"),
    ],
)
def test_out_of_range_pipeline_settings_are_rejected(
    environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    environment.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        load()


def test_an_example_file_lists_every_pipeline_setting() -> None:
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")

    for name in REQUIRED_VARIABLES:
        assert f"\n{name}=" in example


@pytest.mark.parametrize(
    ("deadline", "recording", "reserve"),
    [("560", "20", "30"), ("540", "40", "30"), ("540", "20", "40"), ("600", "1", "10")],
    ids=["deadline", "recording", "reserve", "deadline-alone"],
)
def test_the_job_timeout_must_cover_the_run_recording_and_clean_up(
    environment: pytest.MonkeyPatch, deadline: str, recording: str, reserve: str
) -> None:
    environment.setenv("INDEXING_RUN_DEADLINE_SECONDS", deadline)
    environment.setenv("INDEXING_FAILURE_RECORDING_SECONDS", recording)
    environment.setenv("INDEXING_CLEANUP_RESERVE_SECONDS", reserve)

    with pytest.raises(ValidationError, match="plus indexing_cleanup_reserve_seconds"):
        load()


def test_the_clean_up_reserve_must_cover_stopping_a_media_tool(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("MEDIA_TOOL_STOP_TIMEOUT_SECONDS", "16")

    with pytest.raises(ValidationError, match="twice media_tool_stop_timeout_seconds"):
        load()


@pytest.mark.parametrize("stale_after", ["750", "600"])
def test_stuck_items_are_only_older_than_the_longest_silence_of_a_live_job(
    environment: pytest.MonkeyPatch, stale_after: str
) -> None:
    environment.setenv("INDEXING_STALE_AFTER_SECONDS", stale_after)

    with pytest.raises(ValidationError, match="indexing_stale_after_seconds must be longer"):
        load()


@pytest.mark.parametrize("interval", ["7", "45", "90", "420"])
def test_the_sweep_interval_must_fit_a_cron(environment: pytest.MonkeyPatch, interval: str) -> None:
    environment.setenv("INDEXING_SWEEP_INTERVAL_SECONDS", interval)

    with pytest.raises(ValidationError, match="divide a minute"):
        load()


@pytest.mark.parametrize("interval", ["1", "15", "30", "60", "300", "900", "3600"])
def test_sweep_intervals_that_fit_a_cron_are_accepted(
    environment: pytest.MonkeyPatch, interval: str
) -> None:
    environment.setenv("INDEXING_SWEEP_INTERVAL_SECONDS", interval)

    assert load().indexing_sweep_interval_seconds == int(interval)
