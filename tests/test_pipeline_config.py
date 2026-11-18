from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import PipelineSettings

REQUIRED_VARIABLES = {
    "MEDIA_STORAGE_DIR": "./media",
    "MEDIA_DOWNLOAD_TIMEOUT_SECONDS": "30",
    "MEDIA_MAX_DOWNLOAD_BYTES": "104857600",
    "MEDIA_STORED_HEIGHT_PIXELS": "360",
    "FFMPEG_PATH": "ffmpeg",
    "FFPROBE_PATH": "ffprobe",
    "MEDIA_TOOL_TIMEOUT_SECONDS": "120",
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
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")

    for name in REQUIRED_VARIABLES:
        assert f"\n{name}=" in example


def test_the_job_timeout_must_leave_time_after_the_run_deadline(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("INDEXING_RUN_DEADLINE_SECONDS", "600")

    with pytest.raises(ValidationError, match="longer than indexing_run_deadline_seconds"):
        load()
