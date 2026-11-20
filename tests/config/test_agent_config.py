import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.agent import AgentSettings

ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[2] / ".env.example"
AGENT_ENVIRONMENT = {
    "AGENT_MAX_STEPS": "6",
    "AGENT_MAX_RESULTS_PER_ANSWER": "5",
    "AGENT_MAX_OUTPUT_TOKENS": "600",
    "VOCAB_ALIAS_REVIEW_MIN_CORRECTIONS": "3",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in AGENT_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def load_from_environment_only() -> AgentSettings:
    return AgentSettings(_env_file=None)


def test_agent_settings_are_read_from_environment_variables(
    environment: pytest.MonkeyPatch,
) -> None:
    settings = load_from_environment_only()

    assert settings.agent_max_steps == 6
    assert settings.agent_max_results_per_answer == 5
    assert settings.agent_max_output_tokens == 600
    assert settings.vocab_alias_review_min_corrections == 3


@pytest.mark.parametrize("missing_variable", sorted(AGENT_ENVIRONMENT))
def test_missing_agent_variable_fails_at_load(
    environment: pytest.MonkeyPatch, missing_variable: str
) -> None:
    environment.delenv(missing_variable)

    with pytest.raises(ValidationError, match=missing_variable.lower()):
        load_from_environment_only()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        # One step could never run a tool and then answer.
        ("AGENT_MAX_STEPS", "1"),
        ("AGENT_MAX_RESULTS_PER_ANSWER", "0"),
        ("AGENT_MAX_OUTPUT_TOKENS", "0"),
        ("VOCAB_ALIAS_REVIEW_MIN_CORRECTIONS", "0"),
    ],
)
def test_unusable_agent_setting_is_rejected(
    environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    environment.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        load_from_environment_only()


def test_env_example_lists_every_agent_variable_with_a_loadable_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignments = re.findall(r"^([A-Z_]+)=(.*)$", ENV_EXAMPLE_PATH.read_text(), flags=re.MULTILINE)
    example = dict(assignments)
    assert set(AGENT_ENVIRONMENT) <= set(example)
    for name in AGENT_ENVIRONMENT:
        monkeypatch.setenv(name, example[name])

    load_from_environment_only()
