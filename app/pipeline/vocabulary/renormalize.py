"""Rejecting a wrong vocabulary alias, and normalizing its items again.

`docs/ARCHITECTURE.md` §3.4: a rejected alias is never matched or used again.
Only the items whose normalize step went through it are normalized again, and
only the values that went through it, from the item's own wording in
`raw_values`. Values the user set by hand stay as they are, and no other item,
the user's or anyone else's, is touched. Item counts move with the values.

Corrections by users (SAV-17) are not evidence of a synonym, and don't come
through here.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import SavedItem
from app.ingestion.repository import lock_user
from app.pipeline.vocabulary import store as vocabulary
from app.pipeline.vocabulary.normalize import (
    PROVENANCE_ALIAS_ID,
    PROVENANCE_VALUES,
    RAW_FIELD,
    RAW_USER_SET,
    RAW_WORDING,
    Normalization,
    Normalizer,
)
from app.pipeline.vocabulary.open_values import OpenValue, document_of_item, item_values, set_value
from app.types.registry import ContentTypeRegistry

logger = logging.getLogger(__name__)


class UnknownAliasError(LookupError):
    """No vocabulary alias has the given id."""


class AliasRejection:
    """Rejects a wrong alias and normalizes again only the items that matched through it."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        normalizer: Normalizer,
        registry: ContentTypeRegistry,
        *,
        alias_min_probability: float,
    ) -> None:
        """Create the service.

        Args:
            sessions: Opens a database session.
            normalizer: Normalizes the affected values again.
            registry: The built-in types, to read each item's schema.
            alias_min_probability: `VOCAB_ALIAS_MIN_PROBABILITY`, for verifying
                the aliases the new matches go through.
        """
        self._sessions = sessions
        self._normalizer = normalizer
        self._registry = registry
        self._alias_min_probability = alias_min_probability

    async def reject(self, alias_id: uuid.UUID) -> list[uuid.UUID]:
        """Reject an alias, then normalize its items again from their own wording.

        Only the values that went through the alias change, and only when the
        user did not set them by hand. Other items, the user's or anyone
        else's, are not touched.

        Args:
            alias_id: The wrong alias.

        Returns:
            The items that were normalized again.

        Raises:
            UnknownAliasError: If no alias has that id.
            ModelError: If a decision or embedding call failed.
            sqlalchemy.exc.SQLAlchemyError: If the database failed.
        """
        async with self._sessions() as session, session.begin():
            if not await vocabulary.reject_alias(session, alias_id):
                raise UnknownAliasError(f"no vocabulary alias with id {alias_id}")
            item_ids = await vocabulary.items_matched_through(session, alias_id)
        logger.info(
            "vocabulary_alias_rejected",
            extra={"alias_id": str(alias_id), "items": len(item_ids)},
        )
        for item_id in item_ids:
            await self._normalize_item_again(item_id, alias_id)
        return item_ids

    async def _normalize_item_again(self, item_id: uuid.UUID, alias_id: uuid.UUID) -> None:
        async with self._sessions() as session:
            item = await session.get(SavedItem, item_id)
            run = await vocabulary.normalize_run(session, item_id)
            if item is None or run is None:
                return
            affected = _values_through(alias_id, run.output or {}, item.raw_values)
        if not affected:
            return
        # The models are asked without any lock, so the user may change the item
        # meanwhile. What they changed is read again under the lock and kept.
        normalization = await self._normalizer.normalize(affected)
        applied = await self._apply_unchanged(item_id, alias_id, affected, normalization)
        await self._normalizer.verify_aliases(
            Normalization(
                values={value.path: normalization.values[value.path] for value in applied},
                costs=normalization.costs,
            ).confident_alias_ids(self._alias_min_probability)
        )
        logger.info(
            "saved_item_normalized_again",
            extra={
                "saved_item_id": str(item_id),
                "values": len(applied),
                "changed_meanwhile": len(affected) - len(applied),
            },
        )

    async def _apply_unchanged(
        self,
        item_id: uuid.UUID,
        alias_id: uuid.UUID,
        affected: Sequence[OpenValue],
        normalization: Normalization,
    ) -> list[OpenValue]:
        """Apply the new values whose wording and provenance did not change meanwhile.

        The owner's row is locked before the item is read again, as every other
        writer of the item does. A value the user set by hand, or whose wording
        or provenance changed since the snapshot, stays as it is now.

        Returns:
            The values that were applied.
        """
        async with self._sessions() as session, session.begin():
            owner_id = await vocabulary.owner_of(session, item_id)
            if owner_id is None or not await lock_user(session, owner_id):
                return []
            item = await session.get(SavedItem, item_id, populate_existing=True)
            run = await vocabulary.normalize_run(session, item_id)
            if item is None or run is None:
                return []
            current = set(_values_through(alias_id, run.output or {}, item.raw_values))
            schema = self._registry.extraction_schema(item.type)
            document = document_of_item(
                schema,
                summary=item.summary,
                category=item.category,
                tags=item.tags,
                fields=item.fields,
            )
            applied: list[OpenValue] = []
            for open_value in affected:
                if open_value not in current:
                    continue
                set_value(document, open_value.path, normalization.values[open_value.path].value)
                applied.append(open_value)
            if not applied:
                return []
            await vocabulary.apply_item_values(session, item, item_values(schema, document))
            run.output = _provenance_with(run.output or {}, applied, normalization)
            return applied


def _values_through(
    alias_id: uuid.UUID, output: dict[str, object], raw_values: dict[str, object]
) -> list[OpenValue]:
    provenance = output.get(PROVENANCE_VALUES)
    if not isinstance(provenance, dict):
        return []
    affected: list[OpenValue] = []
    for path, entry in provenance.items():
        raw = raw_values.get(path)
        if (
            not isinstance(entry, dict)
            or entry.get(PROVENANCE_ALIAS_ID) != str(alias_id)
            or not isinstance(raw, dict)
            or raw.get(RAW_USER_SET) is not False
        ):
            continue
        wording = raw.get(RAW_WORDING)
        vocabulary_field = raw.get(RAW_FIELD)
        if isinstance(wording, str) and isinstance(vocabulary_field, str):
            affected.append(OpenValue(path, vocabulary_field, wording, nested=False))
    return affected


def _provenance_with(
    output: dict[str, object], affected: Sequence[OpenValue], normalization: Normalization
) -> dict[str, object]:
    provenance = output.get(PROVENANCE_VALUES)
    entries = dict(provenance) if isinstance(provenance, dict) else {}
    for open_value in affected:
        entries[open_value.path] = normalization.values[open_value.path].provenance(
            open_value.field
        )
    return {**output, PROVENANCE_VALUES: entries}
