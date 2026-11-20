"""Settings of the response agent."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Configuration of the response agent in `app/agent/` (ARCHITECTURE §3.5).

    It is separate from `Settings` so that the parts of the system that never
    answer questions (the webhook, the migrations) do not need these values.
    Like `Settings`, values come from environment variables with `.env` as a
    fallback, no field has a default, and a validation error names the field
    but never repeats its value.

    Attributes:
        agent_max_steps: The most model calls one answer may take, tool rounds
            included (`AGENT_MAX_STEPS`). The last call may not call tools, so
            the agent always ends with an answer or an explicit error.
        agent_max_results_per_answer: The most saved items one answer sends
            back as replies to the saved reels (`AGENT_MAX_RESULTS_PER_ANSWER`,
            decision record 0004). The rest are kept for "show more".
        agent_max_output_tokens: The longest reply the model may write per
            call (`AGENT_MAX_OUTPUT_TOKENS`).
        vocab_alias_review_min_corrections: A verified vocabulary alias with at
            least this many user corrections against it is flagged for review
            (`VOCAB_ALIAS_REVIEW_MIN_CORRECTIONS`, ARCHITECTURE §3.4).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    agent_max_steps: int = Field(ge=2)
    agent_max_results_per_answer: int = Field(ge=1)
    agent_max_output_tokens: int = Field(ge=1)
    vocab_alias_review_min_corrections: int = Field(ge=1)


@lru_cache
def get_agent_settings() -> AgentSettings:
    """Return the process-wide agent settings, read once from the environment.

    Returns:
        The cached `AgentSettings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return AgentSettings()
