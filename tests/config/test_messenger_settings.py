import pytest
from pydantic import ValidationError

from app.config.messenger import MessengerSettings
from tests.agent.support import ACCESS_TOKEN, BOT_ACCOUNT_ID, GRAPH_API_URL

REQUIRED_VARIABLES = {
    "INSTAGRAM_GRAPH_API_URL": GRAPH_API_URL,
    "INSTAGRAM_ACCOUNT_ID": BOT_ACCOUNT_ID,
    "INSTAGRAM_ACCESS_TOKEN": ACCESS_TOKEN,
    "INSTAGRAM_REPLY_WINDOW_HOURS": "24",
    "SEND_API_TIMEOUT_SECONDS": "10",
    "SEND_API_MAX_ATTEMPTS": "3",
    "SEND_API_RETRY_BASE_DELAY_SECONDS": "0.5",
    "SEND_API_RETRY_MAX_DELAY_SECONDS": "8",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in REQUIRED_VARIABLES.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def load_from_environment_only() -> MessengerSettings:
    return MessengerSettings(_env_file=None)


def test_messenger_settings_are_read_from_environment_variables(
    environment: pytest.MonkeyPatch,
) -> None:
    settings = load_from_environment_only()

    assert str(settings.instagram_graph_api_url) == GRAPH_API_URL
    assert settings.instagram_account_id == BOT_ACCOUNT_ID
    assert settings.instagram_access_token.get_secret_value() == ACCESS_TOKEN
    assert settings.instagram_reply_window_hours == 24
    assert settings.send_api_timeout_seconds == 10
    assert settings.send_api_max_attempts == 3
    assert settings.send_api_retry_base_delay_seconds == 0.5
    assert settings.send_api_retry_max_delay_seconds == 8


@pytest.mark.parametrize("missing_variable", sorted(REQUIRED_VARIABLES))
def test_missing_messenger_variable_fails_at_load(
    environment: pytest.MonkeyPatch, missing_variable: str
) -> None:
    environment.delenv(missing_variable)

    with pytest.raises(ValidationError, match=missing_variable.lower()):
        load_from_environment_only()


def test_graph_api_url_must_use_https(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("INSTAGRAM_GRAPH_API_URL", "http://graph.example.test/v27.0")

    with pytest.raises(ValidationError, match="instagram_graph_api_url"):
        load_from_environment_only()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("INSTAGRAM_REPLY_WINDOW_HOURS", "0"),
        ("INSTAGRAM_REPLY_WINDOW_HOURS", "-1"),
        ("INSTAGRAM_REPLY_WINDOW_HOURS", "24.01"),
        ("INSTAGRAM_REPLY_WINDOW_HOURS", "48"),
        ("INSTAGRAM_REPLY_WINDOW_HOURS", "inf"),
        ("INSTAGRAM_REPLY_WINDOW_HOURS", "nan"),
        ("SEND_API_TIMEOUT_SECONDS", "inf"),
        ("SEND_API_TIMEOUT_SECONDS", "nan"),
        ("SEND_API_RETRY_BASE_DELAY_SECONDS", "nan"),
        ("SEND_API_RETRY_MAX_DELAY_SECONDS", "inf"),
        ("SEND_API_MAX_ATTEMPTS", "0"),
        ("SEND_API_TIMEOUT_SECONDS", "-1"),
        ("INSTAGRAM_ACCOUNT_ID", "  "),
    ],
)
def test_impossible_values_are_rejected(
    environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    environment.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        load_from_environment_only()


@pytest.mark.parametrize("hours", ["24", "0.5"])
def test_reply_window_up_to_instagrams_24_hours_is_accepted(
    environment: pytest.MonkeyPatch, hours: str
) -> None:
    environment.setenv("INSTAGRAM_REPLY_WINDOW_HOURS", hours)

    assert load_from_environment_only().instagram_reply_window_hours == float(hours)


def test_longest_wait_below_the_first_is_rejected(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("SEND_API_RETRY_MAX_DELAY_SECONDS", "0.1")

    with pytest.raises(ValidationError, match="send_api_retry_max_delay_seconds"):
        load_from_environment_only()


def test_access_token_never_appears_in_a_validation_error(
    environment: pytest.MonkeyPatch,
) -> None:
    environment.setenv("SEND_API_MAX_ATTEMPTS", "many")

    with pytest.raises(ValidationError) as raised:
        load_from_environment_only()

    assert ACCESS_TOKEN not in str(raised.value)
    assert "many" not in str(raised.value)
