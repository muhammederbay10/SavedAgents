import pytest
from pydantic import ValidationError

from app.config.base import MAX_EMBEDDING_DIMENSION, Settings
from tests.conftest import (
    TEST_DATABASE_URL,
    TEST_META_APP_SECRET,
    TEST_META_WEBHOOK_VERIFY_TOKEN,
    TEST_REDIS_URL,
)

REQUIRED_VARIABLES = {
    "LOG_LEVEL": "WARNING",
    "DATABASE_URL": TEST_DATABASE_URL,
    "REDIS_URL": TEST_REDIS_URL,
    "EMBEDDING_DIMENSION": "1024",
    "META_APP_SECRET": TEST_META_APP_SECRET,
    "META_WEBHOOK_VERIFY_TOKEN": TEST_META_WEBHOOK_VERIFY_TOKEN,
    "COLLECTION_HINT_WINDOW_SECONDS": "30",
    "INGESTION_STATE_TTL_SECONDS": "129600",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Set every required variable in the process environment."""
    for name, value in REQUIRED_VARIABLES.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def load_settings_from_environment_only() -> Settings:
    return Settings(_env_file=None)


def test_settings_are_read_from_environment_variables(environment: pytest.MonkeyPatch) -> None:
    settings = load_settings_from_environment_only()

    assert settings.log_level == "WARNING"
    assert str(settings.database_url) == TEST_DATABASE_URL
    assert str(settings.redis_url) == TEST_REDIS_URL
    assert settings.embedding_dimension == 1024
    assert settings.meta_app_secret.get_secret_value() == TEST_META_APP_SECRET
    assert settings.meta_webhook_verify_token.get_secret_value() == TEST_META_WEBHOOK_VERIFY_TOKEN
    assert settings.collection_hint_window_seconds == 30
    assert settings.ingestion_state_ttl_seconds == 129600


@pytest.mark.parametrize("missing_variable", sorted(REQUIRED_VARIABLES))
def test_missing_required_variable_fails_at_load(
    environment: pytest.MonkeyPatch, missing_variable: str
) -> None:
    environment.delenv(missing_variable)

    with pytest.raises(ValidationError, match=missing_variable.lower()):
        load_settings_from_environment_only()


def test_unknown_log_level_is_rejected(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError, match="log_level"):
        load_settings_from_environment_only()


def test_database_url_must_be_a_postgres_url(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("DATABASE_URL", "mysql://user:password@localhost:3306/db")

    with pytest.raises(ValidationError, match="database_url"):
        load_settings_from_environment_only()


@pytest.mark.parametrize("dimension", ["0", "-8", str(MAX_EMBEDDING_DIMENSION + 1), "large"])
def test_embedding_dimension_the_vector_index_cannot_hold_is_rejected(
    environment: pytest.MonkeyPatch, dimension: str
) -> None:
    environment.setenv("EMBEDDING_DIMENSION", dimension)

    with pytest.raises(ValidationError, match="embedding_dimension"):
        load_settings_from_environment_only()


def test_largest_indexable_embedding_dimension_is_accepted(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("EMBEDDING_DIMENSION", str(MAX_EMBEDDING_DIMENSION))

    assert load_settings_from_environment_only().embedding_dimension == MAX_EMBEDDING_DIMENSION


SYNTHETIC_PASSWORD = "synthetic-pw-9f2c"


# The URLs are short on purpose: Pydantic shortens long rejected values, which
# would hide the password by accident and let this test pass without the fix.
@pytest.mark.parametrize(
    ("variable", "malformed_url"),
    [
        ("DATABASE_URL", f"postgresql://u:{SYNTHETIC_PASSWORD}@h:not-a-port/d"),
        ("DATABASE_URL", f"mysql://u:{SYNTHETIC_PASSWORD}@h/d"),
        ("REDIS_URL", f"redis://u:{SYNTHETIC_PASSWORD}@h:not-a-port/0"),
    ],
)
def test_validation_error_names_the_field_but_hides_the_rejected_value(
    environment: pytest.MonkeyPatch, variable: str, malformed_url: str
) -> None:
    environment.setenv(variable, malformed_url)

    with pytest.raises(ValidationError) as error:
        load_settings_from_environment_only()

    assert variable.lower() in str(error.value)
    assert SYNTHETIC_PASSWORD not in str(error.value)
    assert "input_value" not in str(error.value)


@pytest.mark.parametrize("variable", ["META_APP_SECRET", "META_WEBHOOK_VERIFY_TOKEN"])
def test_empty_webhook_secret_is_rejected(environment: pytest.MonkeyPatch, variable: str) -> None:
    environment.setenv(variable, "")

    with pytest.raises(ValidationError, match=variable.lower()):
        load_settings_from_environment_only()


def test_webhook_secrets_are_not_shown_when_settings_are_printed(
    environment: pytest.MonkeyPatch,
) -> None:
    printed_settings = repr(load_settings_from_environment_only())

    assert TEST_META_APP_SECRET not in printed_settings
    assert TEST_META_WEBHOOK_VERIFY_TOKEN not in printed_settings


@pytest.mark.parametrize("window", ["0", "-5", "soon"])
def test_collection_hint_window_must_be_a_positive_number_of_seconds(
    environment: pytest.MonkeyPatch, window: str
) -> None:
    environment.setenv("COLLECTION_HINT_WINDOW_SECONDS", window)

    with pytest.raises(ValidationError, match="collection_hint_window_seconds"):
        load_settings_from_environment_only()


@pytest.mark.parametrize("lifetime", ["0", "-1", "a day"])
def test_ingestion_state_lifetime_must_be_a_positive_number_of_seconds(
    environment: pytest.MonkeyPatch, lifetime: str
) -> None:
    environment.setenv("INGESTION_STATE_TTL_SECONDS", lifetime)

    with pytest.raises(ValidationError, match="ingestion_state_ttl_seconds"):
        load_settings_from_environment_only()


def test_ingestion_state_must_outlive_the_hint_window(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("COLLECTION_HINT_WINDOW_SECONDS", "60")
    environment.setenv("INGESTION_STATE_TTL_SECONDS", "59")

    with pytest.raises(ValidationError, match="ingestion_state_ttl_seconds"):
        load_settings_from_environment_only()
