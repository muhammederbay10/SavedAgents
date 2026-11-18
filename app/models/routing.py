"""Which provider served a chat completion, read from OpenRouter's routing metadata.

A request that sends the header `X-OpenRouter-Metadata: enabled` gets an
`openrouter_metadata` object next to the rest of the response (OpenRouter docs,
"Router metadata", read 18 Nov 2026). The parts used here:

    "openrouter_metadata": {
      "endpoints": {"total": 1, "available": [
        {"provider": "CoreWeave", "model": "deepseek/deepseek-v4.1-flash", "selected": true}
      ]},
      "attempt": 1,
      "attempts": [{"provider": "CoreWeave", "model": "...", "status": 200}]
    }

`available` lists the endpoints the router considered, and the one marked
`selected` served the request. `attempt` is the 1-based number of the attempt
that succeeded: above 1 means earlier attempts failed and the router fell back.
`attempts` is the history of those attempts, one entry per upstream call, and
the guide only promises it "when the router retried". Cache replays never carry
the object. Other fields may be added at any time, so unknown fields are ignored.

An endpoint that was only a candidate (`selected: false`) says nothing about
where the request went. What it reached is the selected endpoint and the
attempt history, so those are what the pin is checked against.

`verify_pinned_route` checks a reply against a pin and fails closed: missing,
malformed, ambiguous or contradictory evidence is a `RouteEvidenceError`, never
a pass. `selected_provider` reads the same evidence leniently, for models that
are not pinned and only report the provider for the record.
"""

from dataclasses import dataclass
from http import HTTPStatus

from app.models.wire import JsonObject, list_or_none, object_or_none, string_or_none

ROUTING_METADATA_HEADER = "X-OpenRouter-Metadata"
ROUTING_METADATA_ENABLED = "enabled"

_METADATA_FIELD = "openrouter_metadata"
_FIRST_ATTEMPT = 1


class RouteEvidenceError(Exception):
    """The response does not prove that the pinned provider served it.

    Attributes:
        problem: What is wrong with the evidence, in fixed words with no
            response content.
        actual_provider: The provider the evidence names instead, if it names one.
    """

    def __init__(self, problem: str, actual_provider: str | None = None) -> None:
        """Create the error.

        Args:
            problem: What is wrong with the evidence.
            actual_provider: The provider the evidence names instead, if any.
        """
        super().__init__(problem)
        self.problem = problem
        self.actual_provider = actual_provider


@dataclass(frozen=True)
class _Endpoint:
    provider: str
    model: str
    selected: bool


@dataclass(frozen=True)
class _Attempt:
    provider: str
    model: str
    status: int


def verify_pinned_route(body: JsonObject, *, provider_name: str, model: str) -> str:
    """Check that the pinned provider, and only it, handled a reply.

    Every one of these must hold:

    - the reply carries routing metadata;
    - exactly one endpoint is selected, and it is the pinned provider serving
      the configured model;
    - the attempt number is a JSON integer of 1 or more;
    - every attempt in the history was made to the pinned provider and the
      configured model. A failed attempt elsewhere still means the request
      was sent elsewhere;
    - after more than one attempt, the history is complete: one entry per
      attempt, the last one successful. Without it the earlier attempts are
      unidentified, so the routing is not proven;
    - a top-level `provider` field, if present, names the same provider.

    Endpoints that were only candidates (`selected: false`) are not checked:
    they were not called.

    Args:
        body: The chat completion response.
        provider_name: The pinned provider's name, compared without regard to case.
        model: The configured model id, compared exactly.

    Returns:
        The provider name as the selected endpoint reports it.

    Raises:
        RouteEvidenceError: If any of the conditions above does not hold.
    """
    metadata = object_or_none(body.get(_METADATA_FIELD))
    if metadata is None:
        raise RouteEvidenceError(
            "the reply carries no routing metadata (for example a cache replay)"
        )
    endpoints = _endpoints(metadata)
    selected = [endpoint for endpoint in endpoints if endpoint.selected]
    if not selected:
        raise RouteEvidenceError("the routing metadata has no selected endpoint")
    if len(selected) > 1:
        raise RouteEvidenceError("the routing metadata has more than one selected endpoint")
    served_by = selected[0]
    if not _same_provider(served_by.provider, provider_name):
        raise RouteEvidenceError("another provider served the reply", served_by.provider)
    if served_by.model != model:
        raise RouteEvidenceError("the selected endpoint serves another model", served_by.provider)
    _verify_attempt_history(metadata, provider_name=provider_name, model=model)
    top_level_provider = body.get("provider")
    if top_level_provider is not None and not (
        isinstance(top_level_provider, str) and _same_provider(top_level_provider, provider_name)
    ):
        raise RouteEvidenceError(
            "the response's provider field contradicts its routing metadata",
            string_or_none(top_level_provider),
        )
    return served_by.provider


def selected_provider(body: JsonObject) -> str | None:
    """Return the provider of the one selected endpoint, if the metadata names it.

    Args:
        body: The chat completion response.

    Returns:
        The selected provider's name; `None` if the metadata is missing,
        malformed or names no single selected endpoint.
    """
    metadata = object_or_none(body.get(_METADATA_FIELD))
    if metadata is None:
        return None
    try:
        selected = [endpoint for endpoint in _endpoints(metadata) if endpoint.selected]
    except RouteEvidenceError:
        return None
    return selected[0].provider if len(selected) == 1 else None


def _endpoints(metadata: JsonObject) -> list[_Endpoint]:
    endpoints = object_or_none(metadata.get("endpoints"))
    available = list_or_none(endpoints.get("available")) if endpoints is not None else None
    if available is None:
        raise RouteEvidenceError("the routing metadata's endpoint list is malformed")
    return [_endpoint(raw_endpoint) for raw_endpoint in available]


def _endpoint(raw_endpoint: object) -> _Endpoint:
    entry = object_or_none(raw_endpoint)
    provider = string_or_none(entry.get("provider")) if entry is not None else None
    model = string_or_none(entry.get("model")) if entry is not None else None
    selected = entry.get("selected") if entry is not None else None
    # Only a JSON boolean counts. "true" or 1 is malformed, not selected.
    if provider is None or model is None or not isinstance(selected, bool):
        raise RouteEvidenceError("an endpoint in the routing metadata is malformed")
    return _Endpoint(provider=provider, model=model, selected=selected)


def _verify_attempt_history(metadata: JsonObject, *, provider_name: str, model: str) -> None:
    attempt_number = _attempt_number(metadata)
    attempts = _attempts(metadata)
    for attempt in attempts:
        if not _same_provider(attempt.provider, provider_name):
            raise RouteEvidenceError("the router tried another provider", attempt.provider)
        if attempt.model != model:
            raise RouteEvidenceError("the router tried another model", attempt.provider)
    if attempt_number == _FIRST_ATTEMPT and not attempts:
        # One attempt, and it is the selected endpoint: nothing else was called.
        return
    if len(attempts) != attempt_number:
        raise RouteEvidenceError(
            "the routing metadata has no complete attempt history for its attempt number"
        )
    if attempts[-1].status != HTTPStatus.OK:
        raise RouteEvidenceError("the last attempt did not succeed")


def _attempt_number(metadata: JsonObject) -> int:
    attempt_number = metadata.get("attempt")
    # Only a JSON integer counts. 1.0, true or "1" is malformed, never converted.
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < _FIRST_ATTEMPT
    ):
        raise RouteEvidenceError("the routing metadata has no valid attempt number")
    return attempt_number


def _attempts(metadata: JsonObject) -> list[_Attempt]:
    raw_attempts = metadata.get("attempts")
    if raw_attempts is None:
        return []
    attempts = list_or_none(raw_attempts)
    if attempts is None:
        raise RouteEvidenceError("the routing metadata's attempt list is malformed")
    return [_attempt(raw_attempt) for raw_attempt in attempts]


def _attempt(raw_attempt: object) -> _Attempt:
    entry = object_or_none(raw_attempt)
    provider = string_or_none(entry.get("provider")) if entry is not None else None
    model = string_or_none(entry.get("model")) if entry is not None else None
    status = entry.get("status") if entry is not None else None
    if provider is None or model is None or isinstance(status, bool) or not isinstance(status, int):
        raise RouteEvidenceError("an attempt in the routing metadata is malformed")
    return _Attempt(provider=provider, model=model, status=status)


def _same_provider(reported: str, pinned: str) -> bool:
    return reported.casefold() == pinned.casefold()
