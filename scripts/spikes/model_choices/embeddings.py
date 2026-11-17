"""Embedding comparison: does a sentence find its translations across languages?

The FLEURS references are the same sentences in every language. Each model
embeds them once at 1024 dimensions and once at its full size, and two checks
run on the vectors:

- **Pairs:** for every ordered language pair, each sentence must rank its own
  translation first among the target language's sentences.
- **All languages:** each sentence is ranked against every sentence in the
  other languages; its translations should fill the top places.
"""

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scripts.spikes.model_choices.fleurs import FleursSample
from scripts.spikes.model_choices.openrouter import JsonObject, OpenRouterClient
from scripts.spikes.model_choices.settings import (
    ESTIMATE_SAFETY_FACTOR,
    REDUCED_EMBEDDING_DIMENSIONS,
    TOKENS_PER_MILLION,
    EmbeddingModel,
)

logger = logging.getLogger(__name__)

SentenceKey = tuple[str, int]
Vector = Sequence[float]


@dataclass(frozen=True)
class RetrievalScore:
    """How well a set of vectors finds translations.

    Attributes:
        pair_correct: Pair queries whose translation ranked first.
        pair_queries: Pair queries asked (language pairs x sentences).
        global_top1_correct: Sentences whose best match in all other languages is a translation.
        global_queries: Sentences ranked against all other languages.
        global_candidates: Candidates per global query.
        all_translations_on_top: Sentences whose translations fill the top places exactly.
    """

    pair_correct: int
    pair_queries: int
    global_top1_correct: int
    global_queries: int
    global_candidates: int
    all_translations_on_top: int


@dataclass(frozen=True)
class EmbeddingRun:
    """One model at one vector size.

    Attributes:
        model_id: The exact OpenRouter model id.
        requested_dimensions: The `dimensions` value sent, or `None` for the full size.
        expected_dimensions: The vector size the response must have.
        status: HTTP status, or `None` when no response arrived.
        error: The exception's class name when no response arrived.
        latency_seconds: Time for the one batch request.
        returned_cost_usd: The charge the API reported, if any.
        vectors_valid: One finite, non-zero vector of the expected size per text.
        retrieval: The retrieval score; `None` when the vectors were not valid.
    """

    model_id: str
    requested_dimensions: int | None
    expected_dimensions: int
    status: int | None
    error: str | None
    latency_seconds: float
    returned_cost_usd: float | None
    vectors_valid: bool
    retrieval: RetrievalScore | None


def estimate_embedding_usd(model: EmbeddingModel, texts: Sequence[str]) -> float:
    """Return the most one batch is expected to cost.

    A text never has more tokens than UTF-8 bytes, so bytes are a safe upper bound.
    """
    token_upper_bound = sum(len(text.encode("utf-8")) for text in texts)
    price_per_token = model.catalog_usd_per_million_tokens / TOKENS_PER_MILLION
    return token_upper_bound * price_per_token * ESTIMATE_SAFETY_FACTOR


async def run_comparison(
    client: OpenRouterClient,
    models: Sequence[EmbeddingModel],
    samples: Sequence[FleursSample],
    runs: list[EmbeddingRun],
) -> None:
    """Embed the reference sentences with every model, at 1024 dimensions and at full size.

    Args:
        client: The OpenRouter client.
        models: The models to compare.
        samples: The verified FLEURS samples; their references are embedded.
        runs: Filled as requests finish, so a stopped run keeps what it measured.

    Raises:
        SpendCapExceededError: If the next request could pass the spending cap.
    """
    keys = [(sample.language, sample.sentence_id) for sample in samples]
    texts = [sample.reference for sample in samples]
    for model in models:
        for requested_dimensions in (REDUCED_EMBEDDING_DIMENSIONS, None):
            estimate_usd = estimate_embedding_usd(model, texts)
            result = await client.embed(model.model_id, texts, requested_dimensions, estimate_usd)
            expected_dimensions = requested_dimensions or model.full_dimensions
            vectors = parse_vectors(result.body, len(texts), expected_dimensions)
            run = EmbeddingRun(
                model_id=model.model_id,
                requested_dimensions=requested_dimensions,
                expected_dimensions=expected_dimensions,
                status=result.status,
                error=result.error,
                latency_seconds=result.latency_seconds,
                returned_cost_usd=result.returned_cost_usd,
                vectors_valid=vectors is not None,
                retrieval=score_retrieval(dict(zip(keys, vectors, strict=True)))
                if vectors
                else None,
            )
            runs.append(run)
            logger.info(
                "embedding_finished",
                extra={
                    "model": run.model_id,
                    "dimensions": run.expected_dimensions,
                    "status": run.status,
                    "vectors_valid": run.vectors_valid,
                },
            )


def parse_vectors(
    body: JsonObject, expected_count: int, expected_dimensions: int
) -> list[list[float]] | None:
    """Read the vectors from an embeddings response, in input order.

    Args:
        body: The response's JSON object.
        expected_count: How many texts were sent.
        expected_dimensions: The size every vector must have.

    Returns:
        The vectors ordered by `index`, or `None` unless there is exactly one
        finite, non-zero vector of the expected size for every text.
    """
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        return None
    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        index = item.get("index")
        vector = _valid_vector(item.get("embedding"), expected_dimensions)
        if not isinstance(index, int) or vector is None:
            return None
        by_index[index] = vector
    if sorted(by_index) != list(range(expected_count)):
        return None
    return [by_index[index] for index in range(expected_count)]


def _valid_vector(value: object, expected_dimensions: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != expected_dimensions:
        return None
    if not all(
        isinstance(number, int | float) and not isinstance(number, bool) for number in value
    ):
        return None
    vector = [float(number) for number in value]
    if not all(math.isfinite(number) for number in vector) or not any(vector):
        return None
    return vector


def cosine_similarity(first: Vector, second: Vector) -> float:
    """Return the cosine similarity of two non-zero vectors of the same size."""
    dot_product = sum(a * b for a, b in zip(first, second, strict=True))
    return dot_product / (math.hypot(*first) * math.hypot(*second))


def score_retrieval(vectors: Mapping[SentenceKey, Vector]) -> RetrievalScore:
    """Score cross-language retrieval for one set of vectors.

    Args:
        vectors: One vector per (language, sentence id). Every language must
            hold the same sentence ids.

    Returns:
        The pair and all-language retrieval counts.
    """
    languages = sorted({language for language, _ in vectors})
    translations_per_sentence = len(languages) - 1
    pair_correct = 0
    pair_queries = 0
    global_top1_correct = 0
    all_translations_on_top = 0
    for query_key, query_vector in vectors.items():
        query_language, sentence_id = query_key
        others = {key: vector for key, vector in vectors.items() if key[0] != query_language}
        for target_language in languages:
            if target_language == query_language:
                continue
            targets = {key: vector for key, vector in others.items() if key[0] == target_language}
            pair_queries += 1
            pair_correct += _ranked(query_vector, targets)[0][1] == sentence_id
        best_sentence_ids = [
            key[1] for key in _ranked(query_vector, others)[:translations_per_sentence]
        ]
        global_top1_correct += best_sentence_ids[0] == sentence_id
        all_translations_on_top += all(found == sentence_id for found in best_sentence_ids)
    return RetrievalScore(
        pair_correct=pair_correct,
        pair_queries=pair_queries,
        global_top1_correct=global_top1_correct,
        global_queries=len(vectors),
        global_candidates=len(vectors) - len(vectors) // len(languages),
        all_translations_on_top=all_translations_on_top,
    )


def _ranked(query: Vector, candidates: Mapping[SentenceKey, Vector]) -> list[SentenceKey]:
    return sorted(
        candidates, key=lambda key: cosine_similarity(query, candidates[key]), reverse=True
    )
