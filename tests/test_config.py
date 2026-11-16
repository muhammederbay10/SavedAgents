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
