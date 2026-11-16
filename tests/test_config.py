import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

REQUIRED_VARIABLES = {
    "LOG_LEVEL": "WARNING",
    "DATABASE_URL": TEST_DATABASE_URL,
    "REDIS_URL": TEST_REDIS_URL,
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
