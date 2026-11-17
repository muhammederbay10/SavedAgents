import math

import pytest

from scripts.spikes.model_choices.embeddings import (
    RetrievalScore,
    cosine_similarity,
    estimate_embedding_usd,
    parse_vectors,
    score_retrieval,
)
from scripts.spikes.model_choices.settings import EmbeddingModel

LANGUAGES = ("en_us", "tr_tr", "ja_jp")
SENTENCE_IDS = (1, 2, 3)


def aligned_vectors() -> dict[tuple[str, int], list[float]]:
    """Each sentence points along its own axis; the language only adds a small offset."""
    vectors = {}
    for language_index, language in enumerate(LANGUAGES):
        for axis, sentence_id in enumerate(SENTENCE_IDS):
            vector = [0.05 * language_index] * len(SENTENCE_IDS)
            vector[axis] = 1.0
            vectors[(language, sentence_id)] = vector
    return vectors


def embeddings_body(vectors: list[list[float]]) -> dict[str, object]:
    return {"data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)]}


def test_cosine_similarity_of_parallel_and_orthogonal_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [3.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 2.0]) == pytest.approx(0.0)


def test_every_translation_ranks_first_when_vectors_are_aligned() -> None:
    assert score_retrieval(aligned_vectors()) == RetrievalScore(
        pair_correct=18,
        pair_queries=18,
        global_top1_correct=9,
        global_queries=9,
        global_candidates=6,
        all_translations_on_top=9,
    )


def test_wrong_translation_is_counted_as_a_miss() -> None:
    vectors = aligned_vectors()
    # The Japanese sentence 3 now looks like sentence 1.
    vectors[("ja_jp", 3)] = vectors[("ja_jp", 1)]

    score = score_retrieval(vectors)

    assert score.pair_queries == 18
    assert score.pair_correct < 18
    assert score.global_top1_correct < 9
    assert score.all_translations_on_top < 9


def test_vectors_are_returned_in_input_order() -> None:
    body: dict[str, object] = {
        "data": [
            {"index": 1, "embedding": [0.0, 2.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    assert parse_vectors(body, expected_count=2, expected_dimensions=2) == [[1.0, 0.0], [0.0, 2.0]]


@pytest.mark.parametrize(
    "vectors",
    [
        pytest.param([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], id="wrong size"),
        pytest.param([[1.0, 0.0]], id="a vector is missing"),
        pytest.param([[1.0, 0.0], [0.0, 0.0]], id="all zeros"),
        pytest.param([[1.0, 0.0], [math.nan, 1.0]], id="not a number"),
        pytest.param([[1.0, 0.0], [math.inf, 1.0]], id="infinite"),
    ],
)
def test_invalid_vectors_are_rejected(vectors: list[list[float]]) -> None:
    assert parse_vectors(embeddings_body(vectors), expected_count=2, expected_dimensions=2) is None


def test_repeated_index_is_rejected() -> None:
    body: dict[str, object] = {
        "data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 0, "embedding": [0.0, 1.0]},
        ]
    }
    assert parse_vectors(body, expected_count=2, expected_dimensions=2) is None


def test_error_body_has_no_vectors() -> None:
    assert parse_vectors({"error": {"message": "nope"}}, 2, 2) is None


def test_estimate_treats_every_byte_as_a_token_and_doubles_the_price() -> None:
    model = EmbeddingModel("vendor/embedder", 8, catalog_usd_per_million_tokens=1.0)
    assert estimate_embedding_usd(model, ["abcd", "é"]) == pytest.approx(12 / 1_000_000)
