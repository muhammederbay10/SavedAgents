"""Step 6, embed: one vector for what the post is about and what was said in it.

The summary and the transcript are embedded together, so vector search finds
the item by meaning, in any language (`docs/ARCHITECTURE.md` §3.3, §3.5).
"""

from dataclasses import dataclass, field
from typing import Final

from app.models.base import Embedder
from app.pipeline.costs import CallCosts

_SECTION_BREAK: Final = "\n\n"


@dataclass(frozen=True)
class ItemEmbedding:
    """An item's vector.

    Attributes:
        vector: The embedding.
        model: The model that made it.
        costs: The call's charge.
    """

    vector: list[float] = field(repr=False)
    model: str
    costs: CallCosts


class ItemEmbedder:
    """Embeds an item's summary and transcript."""

    def __init__(self, embedder: Embedder) -> None:
        """Create the step.

        Args:
            embedder: The embedding model.
        """
        self._embedder = embedder

    async def embed(self, summary: str, transcript: str | None) -> ItemEmbedding:
        """Embed one item.

        Args:
            summary: The item's summary.
            transcript: What was said, when the item has speech.

        Returns:
            The item's vector.

        Raises:
            ModelError: If the call failed or the vector had the wrong size.
        """
        text = summary if not transcript else summary + _SECTION_BREAK + transcript
        result = await self._embedder.embed([text])
        return ItemEmbedding(
            vector=list(result.vectors[0]),
            model=result.model,
            costs=CallCosts().add(result.cost_usd),
        )
