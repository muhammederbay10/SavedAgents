import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.retrieval import RetrievalSettings

ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[2] / ".env.example"
RETRIEVAL_ENVIRONMENT = {
    "SEARCH_TIME_BUDGET_SECONDS": "10",
    "SEARCH_MAX_RESULTS": "20",
    "SEARCH_MIN_RESULTS": "3",
    "FILTER_NORMALIZATION_MIN_PROBABILITY": "0.6",
    "FILTER_NORMALIZATION_MAX_OPTIONS": "20",
    "AVAILABILITY_MAX_AGE_DAYS": "7",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in RETRIEVAL_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def load_from_environment_only() -> RetrievalSettings:
    return RetrievalSettings(_env_file=None)


def test_retrieval_settings_are_read_from_environment_variables(
    environment: pytest.MonkeyPatch,
) -> None:
    settings = load_from_environment_only()

    assert settings.search_time_budget_seconds == 10.0
    assert settings.search_max_results == 20
    assert settings.search_min_results == 3
    assert settings.filter_normalization_min_probability == 0.6
    assert settings.filter_normalization_max_options == 20
    assert settings.availability_max_age_days == 7.0


@pytest.mark.parametrize("missing_variable", sorted(RETRIEVAL_ENVIRONMENT))
def test_missing_retrieval_variable_fails_at_load(
    environment: pytest.MonkeyPatch, missing_variable: str
) -> None:
    environment.delenv(missing_variable)

    with pytest.raises(ValidationError, match=missing_variable.lower()):
        load_from_environment_only()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SEARCH_TIME_BUDGET_SECONDS", "0"),
        ("SEARCH_TIME_BUDGET_SECONDS", "inf"),
        ("SEARCH_MAX_RESULTS", "0"),
        ("SEARCH_MIN_RESULTS", "-1"),
        ("FILTER_NORMALIZATION_MIN_PROBABILITY", "1.5"),
        ("FILTER_NORMALIZATION_MAX_OPTIONS", "0"),
        ("AVAILABILITY_MAX_AGE_DAYS", "0"),
    ],
)
def test_unusable_retrieval_setting_is_rejected(
    environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    environment.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        load_from_environment_only()


def test_minimum_results_above_the_maximum_are_rejected(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("SEARCH_MIN_RESULTS", "21")

    with pytest.raises(ValidationError, match="search_min_results"):
        load_from_environment_only()


def test_env_example_lists_every_retrieval_variable_with_a_loadable_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = re.findall(r"^([A-Z_]+)=(.*)$", ENV_EXAMPLE_PATH.read_text(), flags=re.MULTILINE)
    example = dict(assignments)
    assert set(RETRIEVAL_ENVIRONMENT) <= set(example)
    for name in RETRIEVAL_ENVIRONMENT:
        monkeypatch.setenv(name, example[name])

    load_from_environment_only()
