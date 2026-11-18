import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import MAX_EMBEDDING_DIMENSION, ModelSettings
from tests.model_environment import MODEL_ENVIRONMENT

ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"
SYNTHETIC_SECRET = "synthetic-secret-5e8a"


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Set every model variable in the process environment."""
    for name, value in MODEL_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def load_model_settings_from_environment_only() -> ModelSettings:
    return ModelSettings(_env_file=None)


def variables_in_env_example() -> dict[str, str]:
    assignments = re.findall(r"^([A-Z_]+)=(.*)$", ENV_EXAMPLE_PATH.read_text(), flags=re.MULTILINE)
    return dict(assignments)


def test_model_settings_are_read_from_environment_variables(
    environment: pytest.MonkeyPatch,
) -> None:
    settings = load_model_settings_from_environment_only()

    assert settings.openrouter_api_key.get_secret_value() == "synthetic-key-3b1f"
    assert str(settings.openrouter_base_url) == "https://openrouter.test/api/v1"
    assert str(settings.openrouter_decisions_url) == "https://openrouter.test/api/alpha/decisions"
    assert settings.decision_model == "google/gemini-decision-1"
    assert settings.text_model == "deepseek/deepseek-v4.1-flash"
    assert settings.text_model_provider == "coreweave/fp8"
    assert settings.text_model_provider_name == "CoreWeave"
    assert settings.escalation_model == "google/gemini-2.5-flash"
    assert settings.transcription_model == "microsoft/mai-transcribe-2"
    assert settings.transcription_response_format == "verbose_json"
    assert settings.transcription_fallback_model == "openai/gpt-transcribe"
    assert settings.transcription_fallback_response_format == "json"
    assert settings.embedding_model == "openai/text-embedding-3-large"
    assert settings.embedding_dimension == 1024
    assert settings.openrouter_max_attempts == 3
    assert settings.escalation_timeout_seconds == 90.0


@pytest.mark.parametrize("missing_variable", sorted(MODEL_ENVIRONMENT))
def test_missing_model_variable_fails_at_load(
    environment: pytest.MonkeyPatch, missing_variable: str
) -> None:
    environment.delenv(missing_variable)

    with pytest.raises(ValidationError, match=missing_variable.lower()):
        load_model_settings_from_environment_only()


@pytest.mark.parametrize("variable", ["TEXT_MODEL_PROVIDER", "TEXT_MODEL_PROVIDER_NAME"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_provider_pin_cannot_be_left_blank(
    environment: pytest.MonkeyPatch, variable: str, blank: str
) -> None:
    environment.setenv(variable, blank)

    with pytest.raises(ValidationError, match=variable.lower()):
        load_model_settings_from_environment_only()


@pytest.mark.parametrize("variable", ["OPENROUTER_BASE_URL", "OPENROUTER_DECISIONS_URL"])
def test_endpoint_without_https_is_rejected(environment: pytest.MonkeyPatch, variable: str) -> None:
    environment.setenv(variable, "http://openrouter.test/api/v1")

    with pytest.raises(ValidationError, match=variable.lower()):
        load_model_settings_from_environment_only()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OPENROUTER_MAX_ATTEMPTS", "0"),
        ("OPENROUTER_RETRY_BASE_DELAY_SECONDS", "0"),
        ("OPENROUTER_RETRY_MAX_DELAY_SECONDS", "-1"),
        ("DECISION_TIMEOUT_SECONDS", "0"),
        ("TEXT_TIMEOUT_SECONDS", "-5"),
        ("ESCALATION_TIMEOUT_SECONDS", "soon"),
        ("TRANSCRIPTION_TIMEOUT_SECONDS", "0"),
        ("EMBEDDING_TIMEOUT_SECONDS", "0"),
        ("EMBEDDING_DIMENSION", str(MAX_EMBEDDING_DIMENSION + 1)),
        ("TRANSCRIPTION_RESPONSE_FORMAT", "srt"),
        ("TRANSCRIPTION_FALLBACK_RESPONSE_FORMAT", "text"),
        ("DECISION_MODEL", " "),
        ("OPENROUTER_API_KEY", ""),
    ],
)
def test_unusable_model_setting_is_rejected(
    environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    environment.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        load_model_settings_from_environment_only()


def test_longest_wait_below_the_first_wait_is_rejected(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("OPENROUTER_RETRY_BASE_DELAY_SECONDS", "10")
    environment.setenv("OPENROUTER_RETRY_MAX_DELAY_SECONDS", "2")

    with pytest.raises(ValidationError, match="openrouter_retry_max_delay_seconds"):
        load_model_settings_from_environment_only()


def test_api_key_is_hidden_in_repr_and_in_validation_errors(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("OPENROUTER_API_KEY", SYNTHETIC_SECRET)
    assert SYNTHETIC_SECRET not in repr(load_model_settings_from_environment_only())

    environment.setenv("OPENROUTER_BASE_URL", f"ftp://{SYNTHETIC_SECRET}@h/")
    with pytest.raises(ValidationError) as error:
        load_model_settings_from_environment_only()

    assert SYNTHETIC_SECRET not in str(error.value)


def test_env_example_lists_every_model_variable_with_a_loadable_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = variables_in_env_example()
    assert set(MODEL_ENVIRONMENT) <= set(example)
    for name in MODEL_ENVIRONMENT:
        monkeypatch.setenv(name, example[name])

    settings = load_model_settings_from_environment_only()

    assert settings.openrouter_api_key.get_secret_value() == "change-me"
    assert settings.embedding_dimension == 1024
