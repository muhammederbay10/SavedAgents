"""Error types for model calls.

Messages name the operation, the model and the HTTP status. They never carry
request or response bodies, because those hold users' captions, transcripts
and messages, and a provider's own error text can quote its input back.

    ModelError
    ├── ModelRequestError              the request was rejected; retrying won't help
    │   ├── ModelAuthenticationError   the API key was refused (HTTP 401)
    │   └── ModelCreditsError          the account is out of credits (HTTP 402)
    ├── ModelRateLimitError            still rate-limited after every attempt
    ├── ModelUnavailableError          timeouts and server errors after every attempt
    │   └── ProviderRouteUnavailableError  the pinned provider cannot serve the call
    ├── ModelResponseError             an answer arrived but was unusable
    │   ├── SchemaOutputError          the reply was not the JSON that was asked for
    │   ├── EmbeddingDimensionError    a vector had the wrong size
    │   └── ProviderPinViolationError  no proof that the pinned provider alone answered
    └── TranscriptionFailedError       no transcription model produced a transcript
"""

from app.models.base import TranscriptionAttempt


class ModelError(Exception):
    """Base class of every error a model client raises.

    Attributes:
        operation: The interface method that failed: `decide`, `generate`,
            `transcribe` or `embed`.
        model: The configured id of the model that was called.
        status: The HTTP status, or the provider's error code; `None` when no
            response arrived.
        error_type: The provider's short error label, such as
            `rate_limit_exceeded`, when it sent one.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        model: str,
        status: int | None = None,
        error_type: str | None = None,
    ) -> None:
        """Create the error.

        Args:
            message: What went wrong, without any request or response content.
            operation: The interface method that failed.
            model: The configured id of the model that was called.
            status: The HTTP status or provider error code, if there was one.
            error_type: The provider's short error label, if it sent one.
        """
        super().__init__(message)
        self.operation = operation
        self.model = model
        self.status = status
        self.error_type = error_type


class ModelRequestError(ModelError):
    """The provider rejected the request, and sending it again would not help."""


class ModelAuthenticationError(ModelRequestError):
    """The provider refused the API key."""


class ModelCreditsError(ModelRequestError):
    """The account or key has no credits left."""


class ModelRateLimitError(ModelError):
    """The call was still rate-limited after the last attempt."""


class ModelUnavailableError(ModelError):
    """The call timed out or the provider failed, on every attempt."""


class ProviderRouteUnavailableError(ModelUnavailableError):
    """The pinned provider cannot serve the call, and no other provider is allowed.

    Attributes:
        provider: The provider slug the model is pinned to.
    """

    def __init__(self, *, operation: str, model: str, provider: str, status: int | None) -> None:
        """Create the error.

        Args:
            operation: The interface method that failed.
            model: The configured id of the model that was called.
            provider: The provider slug the model is pinned to.
            status: The HTTP status the router answered with.
        """
        super().__init__(
            f"{operation} with {model} failed: no route through the pinned provider "
            f"'{provider}' (HTTP {status}), and falling back to another provider is not allowed",
            operation=operation,
            model=model,
            status=status,
        )
        self.provider = provider


class ModelResponseError(ModelError):
    """A response arrived, but it was not usable."""


class SchemaOutputError(ModelResponseError):
    """The model was asked for a JSON object and returned something else."""


class EmbeddingDimensionError(ModelResponseError):
    """A returned vector did not have the configured number of dimensions.

    Attributes:
        expected: The configured number of dimensions.
        actual: The length of the vector that came back.
    """

    def __init__(self, *, model: str, expected: int, actual: int) -> None:
        """Create the error.

        Args:
            model: The configured id of the embedding model.
            expected: The configured number of dimensions.
            actual: The length of the vector that came back.
        """
        super().__init__(
            f"embed with {model} returned a vector of {actual} dimensions, expected {expected}",
            operation="embed",
            model=model,
        )
        self.expected = expected
        self.actual = actual


class ProviderPinViolationError(ModelResponseError):
    """The response does not prove that the pinned provider, and only it, served the call.

    The reply is discarded: it must not be used, because the request may have
    left the approved host, or there is no evidence that it did not.

    Attributes:
        expected_provider: The provider name the pin expects.
        actual_provider: The other provider the evidence names; `None` if it
            names none.
        problem: What is wrong with the evidence.
    """

    def __init__(
        self,
        *,
        operation: str,
        model: str,
        expected_provider: str,
        actual_provider: str | None,
        problem: str,
    ) -> None:
        """Create the error.

        Args:
            operation: The interface method that failed.
            model: The configured id of the model that was called.
            expected_provider: The provider name the pin expects.
            actual_provider: The other provider the evidence names, if any.
            problem: What is wrong with the evidence, without response content.
        """
        named = f" ('{actual_provider}')" if actual_provider is not None else ""
        super().__init__(
            f"{operation} with {model} is not proven to be served by the pinned provider "
            f"'{expected_provider}': {problem}{named}",
            operation=operation,
            model=model,
        )
        self.expected_provider = expected_provider
        self.actual_provider = actual_provider
        self.problem = problem


class TranscriptionFailedError(ModelError):
    """No transcription model produced a transcript.

    This is different from an empty transcript, which is a valid result and is
    returned, not raised.

    Attributes:
        attempts: Every model that was asked, in order, with how it failed.
    """

    def __init__(self, *, model: str, attempts: tuple[TranscriptionAttempt, ...]) -> None:
        """Create the error.

        Args:
            model: The configured id of the model that was asked last.
            attempts: Every model that was asked, in order.
        """
        tried = ", ".join(f"{attempt.model} ({attempt.error})" for attempt in attempts)
        super().__init__(
            f"transcribe failed on every model: {tried}",
            operation="transcribe",
            model=model,
        )
        self.attempts = attempts
