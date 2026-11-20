"""Settings of the Instagram Send API client."""

from functools import lru_cache
from typing import Annotated, Self

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    StringConstraints,
    UrlConstraints,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# Instagram accepts a reply only this long after the user's last message
# (Instagram "Messaging API" page). A setting can shorten the window, never widen it.
MAX_INSTAGRAM_REPLY_WINDOW_HOURS = 24.0


class MessengerSettings(BaseSettings):
    """Configuration of the Instagram Send API client in `app/agent/replies/messenger.py`.

    It is separate from `Settings` so that the parts of the system that send no
    DM (the health endpoint, the migrations) do not need the bot's access
    token. Like `Settings`, values come from environment variables with `.env`
    as a fallback, no field has a default, and a validation error names the
    field but never repeats its value.

    Attributes:
        instagram_graph_api_url: Base URL of the Graph API, with its version,
            e.g. `https://graph.facebook.com/v24.0` (`INSTAGRAM_GRAPH_API_URL`).
            Messages are posted to `<this URL>/<account id>/messages`.
        instagram_account_id: The bot's Instagram professional account id, the
            `{ig-user-id}` of the Send API (`INSTAGRAM_ACCOUNT_ID`).
        instagram_access_token: The bot's access token for the Send API
            (`INSTAGRAM_ACCESS_TOKEN`). It is sent in a header, never in a URL.
        instagram_reply_window_hours: How long after the user's last message
            the bot may still reply (`INSTAGRAM_REPLY_WINDOW_HOURS`), at most
            `MAX_INSTAGRAM_REPLY_WINDOW_HOURS` (Instagram's 24 hours). A message
            for an older conversation is skipped. Every number here must be finite.
        send_api_timeout_seconds: Time limit of one Send API request
            (`SEND_API_TIMEOUT_SECONDS`).
        send_api_max_attempts: How often one message is tried before it fails,
            including the first try (`SEND_API_MAX_ATTEMPTS`).
        send_api_retry_base_delay_seconds: The wait before the second try. It
            doubles before each later one (`SEND_API_RETRY_BASE_DELAY_SECONDS`).
        send_api_retry_max_delay_seconds: The longest wait before any try, also
            when Meta asks for a longer one (`SEND_API_RETRY_MAX_DELAY_SECONDS`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # The access token travels in a header of every request, so plain HTTP is refused.
    instagram_graph_api_url: Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https"])]
    instagram_account_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    instagram_access_token: SecretStr = Field(min_length=1)
    instagram_reply_window_hours: float = Field(
        gt=0, le=MAX_INSTAGRAM_REPLY_WINDOW_HOURS, allow_inf_nan=False
    )
    send_api_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    send_api_max_attempts: int = Field(ge=1)
    send_api_retry_base_delay_seconds: float = Field(gt=0, allow_inf_nan=False)
    send_api_retry_max_delay_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _longest_wait_is_not_below_the_first(self) -> Self:
        if self.send_api_retry_max_delay_seconds < self.send_api_retry_base_delay_seconds:
            raise ValueError(
                "send_api_retry_max_delay_seconds must not be below "
                "send_api_retry_base_delay_seconds"
            )
        return self


@lru_cache
def get_messenger_settings() -> MessengerSettings:
    """Return the process-wide Send API settings, read once from the environment.

    Returns:
        The cached `MessengerSettings` instance.

    Raises:
        pydantic.ValidationError: If a required variable is missing or invalid.
    """
    return MessengerSettings()
