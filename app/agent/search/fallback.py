"""The search fallback ladder's levels, why search climbs them, and how each step is logged.

Phase 1 has three levels (ARCHITECTURE §3.6):

- **L0 Normal:** SQL/PostGIS filters, then ranking by vector similarity.
- **L1 Degraded retrieval:** the embedding or the vector search failed, so the
  same filters are combined with Postgres full-text search on the caption,
  summary and transcript.
- **L2 Relax and catch up:** too few results, so the filters are loosened and
  items that are not fully indexed yet are searched by keyword.

Search stops at the first level that answers. Every step up is logged with its
level and reason, so a fallback that happens often is noticed instead of
hiding a bug. L3 and L4 are Phase 2.
"""

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class FallbackLevel(StrEnum):
    """A level of the fallback ladder."""

    NORMAL = "L0"
    DEGRADED = "L1"
    RELAXED = "L2"


class FallbackReason(StrEnum):
    """Why search left the normal path.

    Attributes:
        EMBEDDING_FAILED: The query text could not be embedded.
        VECTOR_SEARCH_FAILED: The database failed the vector search.
        FEW_RESULTS: Fewer results than `SEARCH_MIN_RESULTS`.
        TIME_BUDGET_EXCEEDED: `SEARCH_TIME_BUDGET_SECONDS` ran out, so the
            answer has only what was found by then.
    """

    EMBEDDING_FAILED = "embedding_failed"
    VECTOR_SEARCH_FAILED = "vector_search_failed"
    FEW_RESULTS = "few_results"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"


@dataclass(frozen=True)
class FallbackStep:
    """One step off the normal path.

    Attributes:
        level: The level search moved to, or was at when the budget ran out.
        reason: Why.
    """

    level: FallbackLevel
    reason: FallbackReason


def log_fallback(user_id: uuid.UUID, step: FallbackStep, **details: object) -> None:
    """Log one fallback step, with no query text or other personal data.

    Args:
        user_id: The asking user.
        step: The step.
        **details: Counts and names that explain the step, e.g. `found=0`.
    """
    logger.warning(
        "search_fallback",
        extra={"user_id": str(user_id), "level": step.level, "reason": step.reason, **details},
    )
