"""The embedding client: texts to vectors of one configured size.

Contract (OpenRouter docs, "Create embeddings", 17 Nov 2026): `POST /embeddings`
with `model`, `input` (an array of strings) and `dimensions`. The answer's
`data` holds one entry per input with its `index` and `embedding`.

The database stores vectors of exactly `EMBEDDING_DIMENSION` components, so the
client sends that size and checks every vector that comes back. A vector of
another size is an error here, not a failed insert later.
"""

from collections.abc import Sequence

from app.models.base import Embedder, EmbeddingResult
from app.models.errors import EmbeddingDimensionError, ModelResponseError
from app.models.openrouter import ModelCall, OpenRouterTransport
from app.models.wire import (
    JsonObject,
    cost_usd,
    list_or_none,
    number_or_none,
    object_or_none,
    string_or_none,
)

_OPERATION = "embed"
_ENCODING_FORMAT = "float"


class OpenRouterEmbedder(Embedder):
    """Embeds texts with one model, at one vector size."""

    def __init__(
        self,
        transport: OpenRouterTransport,
        *,
        url: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> None:
        """Create the client.

        Args:
            transport: The shared OpenRouter transport.
            url: The full URL of the embeddings endpoint.
            model: The embedding model's id.
            dimensions: The vector size to ask for and to require in the answer.
            timeout_seconds: How long one attempt may take.
        """
        self._transport = transport
        self._dimensions = dimensions
        self._call = ModelCall(
            operation=_OPERATION, model=model, url=url, timeout_seconds=timeout_seconds
        )

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed a batch of texts in one call.

        Args:
            texts: The texts. At least one.

        Returns:
            One vector per text, in the same order, each of the configured size.

        Raises:
            ValueError: If `texts` is empty.
            EmbeddingDimensionError: If a vector has another size.
            ModelError: If the call failed or the answer was unusable.
        """
        if not texts:
            raise ValueError("embed needs at least one text")
        payload: JsonObject = {
            "model": self._call.model,
            "input": list(texts),
            "dimensions": self._dimensions,
            "encoding_format": _ENCODING_FORMAT,
        }
        body = await self._transport.post(self._call, payload)
        return EmbeddingResult(
            vectors=self._vectors_in_input_order(body, len(texts)),
            model=string_or_none(body.get("model")) or self._call.model,
            cost_usd=cost_usd(body),
        )

    def _vectors_in_input_order(
        self, body: JsonObject, expected_count: int
    ) -> tuple[tuple[float, ...], ...]:
        entries = list_or_none(body.get("data")) or []
        if len(entries) != expected_count:
            raise self._response_error("the response does not hold one embedding per text")
        vectors_by_index: dict[int, tuple[float, ...]] = {}
        for raw_entry in entries:
            entry = object_or_none(raw_entry) or {}
            index = self._index(entry.get("index"), expected_count)
            if index in vectors_by_index:
                raise self._response_error("two embeddings have the same index")
            vectors_by_index[index] = self._vector(entry.get("embedding"))
        return tuple(vectors_by_index[index] for index in range(expected_count))

    def _index(self, raw_index: object, expected_count: int) -> int:
        # Only a JSON integer in range names a text. A float such as 1.9 or 1.0,
        # a boolean or a string is refused, never rounded or converted.
        if (
            isinstance(raw_index, bool)
            or not isinstance(raw_index, int)
            or not 0 <= raw_index < expected_count
        ):
            raise self._response_error("an embedding's index is not an integer in range")
        return raw_index

    def _vector(self, raw_vector: object) -> tuple[float, ...]:
        raw_values = list_or_none(raw_vector)
        if raw_values is None:
            raise self._response_error("an embedding is not a list of numbers")
        if len(raw_values) != self._dimensions:
            raise EmbeddingDimensionError(
                model=self._call.model, expected=self._dimensions, actual=len(raw_values)
            )
        values = [number_or_none(raw_value) for raw_value in raw_values]
        if any(value is None for value in values):
            raise self._response_error("an embedding holds a value that is not a finite number")
        return tuple(value for value in values if value is not None)

    def _response_error(self, problem: str) -> ModelResponseError:
        return ModelResponseError(
            f"{_OPERATION} with {self._call.model} failed: {problem}",
            operation=_OPERATION,
            model=self._call.model,
        )
