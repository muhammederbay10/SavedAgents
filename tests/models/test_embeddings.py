import httpx
import pytest

from app.models.embeddings import OpenRouterEmbedder
from app.models.errors import EmbeddingDimensionError, ModelResponseError
from app.models.openrouter import OpenRouterTransport
from tests.models.conftest import Connect
from tests.models.support import (
    EMBEDDINGS_URL,
    TIMEOUT_SECONDS,
    fixture_response,
    json_response,
    load_fixture,
)

EMBEDDING_MODEL = "openai/text-embedding-3-large"
APPROVED_DIMENSIONS = 1024
FIXTURE_DIMENSIONS = 4
TEXTS = ["first text", "second text"]


def embedder(transport: OpenRouterTransport, dimensions: int) -> OpenRouterEmbedder:
    return OpenRouterEmbedder(
        transport,
        url=EMBEDDINGS_URL,
        model=EMBEDDING_MODEL,
        dimensions=dimensions,
        timeout_seconds=TIMEOUT_SECONDS,
    )


def response_with_vectors(*vectors: list[float]) -> dict[str, object]:
    body = load_fixture("embeddings_response.json")
    body["data"] = [
        {"object": "embedding", "index": index, "embedding": vector}
        for index, vector in enumerate(vectors)
    ]
    return body


async def test_request_asks_for_the_configured_1024_dimensions(connect: Connect) -> None:
    vector = [0.0] * APPROVED_DIMENSIONS
    server, transport = connect([json_response(response_with_vectors(vector, vector))])

    result = await embedder(transport, APPROVED_DIMENSIONS).embed(TEXTS)

    assert server.only_request.url == EMBEDDINGS_URL
    assert server.only_request.body == {
        "model": EMBEDDING_MODEL,
        "input": TEXTS,
        "dimensions": 1024,
        "encoding_format": "float",
    }
    assert [len(vector) for vector in result.vectors] == [1024, 1024]


@pytest.mark.parametrize("returned_dimensions", [3072, 1023, 1025, 0])
async def test_vector_of_another_size_is_rejected(
    connect: Connect, returned_dimensions: int
) -> None:
    right_size = [0.0] * APPROVED_DIMENSIONS
    wrong_size = [0.0] * returned_dimensions
    _, transport = connect([json_response(response_with_vectors(right_size, wrong_size))])

    with pytest.raises(EmbeddingDimensionError) as raised:
        await embedder(transport, APPROVED_DIMENSIONS).embed(TEXTS)

    assert raised.value.expected == 1024
    assert raised.value.actual == returned_dimensions
    assert isinstance(raised.value, ModelResponseError)


async def test_vectors_are_returned_in_the_order_of_the_texts(connect: Connect) -> None:
    _, transport = connect([fixture_response("embeddings_response.json")])

    result = await embedder(transport, FIXTURE_DIMENSIONS).embed(TEXTS)

    assert result.vectors == ((1.0, 0.0, 0.0, 0.0), (0.5, 0.5, 0.5, 0.5))
    assert result.model == EMBEDDING_MODEL
    assert result.cost_usd == 0.00000104


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [0.0] * FIXTURE_DIMENSIONS}],
        [
            {"index": 0, "embedding": [0.0] * FIXTURE_DIMENSIONS},
            {"index": 0, "embedding": [0.0] * FIXTURE_DIMENSIONS},
        ],
        [
            {"index": 0, "embedding": [0.0] * FIXTURE_DIMENSIONS},
            {"index": 2, "embedding": [0.0] * FIXTURE_DIMENSIONS},
        ],
        [
            {"index": 0, "embedding": [0.0] * FIXTURE_DIMENSIONS},
            {"embedding": [0.0] * FIXTURE_DIMENSIONS},
        ],
        [
            {"index": 0, "embedding": [0.0] * FIXTURE_DIMENSIONS},
            {"index": 1, "embedding": "AAAAAA=="},
        ],
        [
            {"index": 0, "embedding": [0.0] * FIXTURE_DIMENSIONS},
            {"index": 1, "embedding": [0.0, 0.0, 0.0, "x"]},
        ],
    ],
)
async def test_response_without_one_usable_vector_per_text_is_rejected(
    connect: Connect, data: list[dict[str, object]]
) -> None:
    body = load_fixture("embeddings_response.json")
    body["data"] = data
    _, transport = connect([json_response(body)])

    with pytest.raises(ModelResponseError):
        await embedder(transport, FIXTURE_DIMENSIONS).embed(TEXTS)


async def test_non_finite_value_in_a_vector_is_rejected(connect: Connect) -> None:
    raw_body = (
        '{"model": "m", "data": ['
        '{"index": 0, "embedding": [0.0, 0.0, 0.0, NaN]},'
        '{"index": 1, "embedding": [0.0, 0.0, 0.0, 0.0]}]}'
    )
    _, transport = connect([httpx.Response(200, text=raw_body)])

    with pytest.raises(ModelResponseError, match="not a finite number"):
        await embedder(transport, FIXTURE_DIMENSIONS).embed(TEXTS)


async def test_empty_batch_is_refused_before_any_request(connect: Connect) -> None:
    server, transport = connect([])

    with pytest.raises(ValueError, match="at least one text"):
        await embedder(transport, FIXTURE_DIMENSIONS).embed([])

    assert server.requests == []
