"""Settings of search over a user's saves."""

from functools import lru_cache
from typing import Self

from pydantic import (
    Field,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetrievalSettings(BaseSettings):
    """Configuration of search over a user's saves in `app/agent/` (ARCHITECTURE §3.5 and §3.6).

    It is separate from `Settings` so that the parts of the system that never
    search (the webhook, the migrations) do not need these values. Like
    `Settings`, values come from environment variables with `.env` as a
    fallback, no field has a default, and a validation error names the field
    but never repeats its value.

    Attributes:
        search_time_budget_seconds: How long one search may take in total,
            model calls and every fallback level included
            (`SEARCH_TIME_BUDGET_SECONDS`). When it runs out, search answers
            with what it has found and marks the answer as incomplete.
        search_max_results: The most items one search returns (`SEARCH_MAX_RESULTS`).
        search_min_results: A search that finds fewer items than this climbs
            to fallback level L2 and relaxes its filters (`SEARCH_MIN_RESULTS`).
        filter_normalization_min_probability: The decision model's lowest
            probability for an option that still counts as what the user meant
            (`FILTER_NORMALIZATION_MIN_PROBABILITY`). Below it, the user's
            wording is kept as written.
        filter_normalization_max_options: The most saved values offered to the
            decision model for one filter (`FILTER_NORMALIZATION_MAX_OPTIONS`).
        availability_max_age_days: Streaming availability older than this is
            reported as stale, so it is fetched again for that country
            (`AVAILABILITY_MAX_AGE_DAYS`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    search_time_budget_seconds: float = Field(gt=0, allow_inf_nan=False)
    search_max_results: int = Field(ge=1)
    search_min_results: int = Field(ge=0)
    filter_normalization_min_probability: float = Field(ge=0, le=1)
    filter_normalization_max_options: int = Field(ge=1)
    availability_max_age_days: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _minimum_fits_in_one_answer(self) -> Self:
        if self.search_min_results > self.search_max_results:
            raise ValueError("search_min_results must not be above search_max_results")
        return self


@lru_cache
def get_retrieval_settings() -> RetrievalSettings:
    """Return the process-wide search settings, read once from the environment.

    Returns:
        The cached `RetrievalSettings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return RetrievalSettings()
