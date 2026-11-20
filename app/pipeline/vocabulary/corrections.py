"""A user's correction of one open value on one of their items (ARCHITECTURE §3.4, record 0005).

"No, it's a travel video" changes **only that item**:

1. The user's wording goes through the normal steps (`Normalizer`): an exact
   hit on a canonical value or verified alias, otherwise the privacy gate, then
   match-or-new. Wording that fails the gate stays on this item only.
2. The item keeps the user's wording in `raw_values`, marked as set by hand, so
   rejecting an alias later never normalizes it again.
3. The correction is counted against the alias that produced the old value, if
   one did. A corrected candidate is never verified; a verified alias that
   collects `VOCAB_ALIAS_REVIEW_MIN_CORRECTIONS` corrections is flagged for
   review in the logs.

A correction is **not** evidence that the old and new values mean the same:
no alias links them, and no other item, the user's or anyone else's, changes.
"""

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AliasStatus, SavedItem
from app.ingestion.repository import lock_user
from app.pipeline.vocabulary import store as vocabulary
from app.pipeline.vocabulary.normalize import (
    PROVENANCE_ALIAS_ID,
    PROVENANCE_VALUES,
    RAW_FIELD,
    RAW_USER_SET,
    RAW_WORDING,
    NormalizedValue,
    NormalizeOutcome,
    Normalizer,
)
from app.pipeline.vocabulary.open_values import (
    JsonDocument,
    OpenValue,
    document_of_item,
    find_open_values,
    item_values,
    set_value,
)
from app.types.base import ExtractionModel
from app.types.registry import ContentTypeRegistry

logger = logging.getLogger(__name__)


class CorrectionError(ValueError):
    """A correction cannot be applied. The message is written for the response agent."""


class UnknownItemError(CorrectionError):
    """The user has no item with that id."""


class UnknownValuePathError(CorrectionError):
    """The item has no open value at that path."""


@dataclass(frozen=True)
class CorrectedValue:
    """What a correction did to one value.

    Attributes:
        path: The value's path, e.g. `category` or `entities.0.cuisine`.
        previous: The value before the correction.
        value: The value the item uses now.
        outcome: How the user's wording was normalized; `private` when it
            failed the privacy gate and stays on this item only.
    """

    path: str
    previous: str = field(repr=False)
    value: str = field(repr=False)
    outcome: NormalizeOutcome


@dataclass(frozen=True)
class _Snapshot:
    schema: type[ExtractionModel]
    open_value: OpenValue


class ItemCorrection:
    """Applies a user's correction to one open value of one of their items."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        normalizer: Normalizer,
        registry: ContentTypeRegistry,
        *,
        alias_min_probability: float,
        review_min_corrections: int,
    ) -> None:
        """Create the service.

        Args:
            sessions: Opens a database session.
            normalizer: Normalizes the user's wording against the vocabulary.
            registry: The built-in types, to read each item's schema.
            alias_min_probability: `VOCAB_ALIAS_MIN_PROBABILITY`, for verifying
                the alias the new value went through.
            review_min_corrections: `VOCAB_ALIAS_REVIEW_MIN_CORRECTIONS`.
        """
        self._sessions = sessions
        self._normalizer = normalizer
        self._registry = registry
        self._alias_min_probability = alias_min_probability
        self._review_min_corrections = review_min_corrections

    async def correct(
        self, user_id: uuid.UUID, item_id: uuid.UUID, path: str, wording: str
    ) -> CorrectedValue:
        """Set one open value of one item to the user's wording, normalized.

        Args:
            user_id: The user who asked. Only their own items can be changed.
            item_id: The item.
            path: The value's path, e.g. `category`, `tags.1` or `subject`.
            wording: The value in the user's own words.

        Returns:
            What changed.

        Raises:
            CorrectionError: If the wording is blank.
            UnknownItemError: If the user has no such item.
            UnknownValuePathError: If the item has no open value at the path.
            ModelError: If a decision or embedding call failed.
            sqlalchemy.exc.SQLAlchemyError: If the database failed.
        """
        if not wording.strip():
            raise CorrectionError("A corrected value must not be blank")
        snapshot = await self._snapshot(user_id, item_id, path)
        corrected = OpenValue(
            path=path,
            field=snapshot.open_value.field,
            wording=wording.strip(),
            nested=snapshot.open_value.nested,
        )
        # The models are asked without any lock; the item is read again under it.
        normalization = await self._normalizer.normalize([corrected])
        normalized = normalization.values[path]
        previous = await self._apply(user_id, item_id, snapshot, corrected, normalized)
        await self._normalizer.verify_aliases(
            normalization.confident_alias_ids(self._alias_min_probability)
        )
        logger.info(
            "saved_item_corrected",
            extra={
                "saved_item_id": str(item_id),
                "field": corrected.field,
                "outcome": normalized.outcome,
            },
        )
        return CorrectedValue(
            path=path, previous=previous, value=normalized.value, outcome=normalized.outcome
        )

    async def _snapshot(self, user_id: uuid.UUID, item_id: uuid.UUID, path: str) -> _Snapshot:
        async with self._sessions() as session:
            item = await _owned_item(session, user_id, item_id)
            schema = self._registry.extraction_schema(item.type)
            open_value = _open_value_at(schema, _document(schema, item), path)
        return _Snapshot(schema=schema, open_value=open_value)

    async def _apply(
        self,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        snapshot: _Snapshot,
        corrected: OpenValue,
        normalized: NormalizedValue,
    ) -> str:
        """Put the new value on the item under the owner's lock; return the previous value."""
        async with self._sessions() as session, session.begin():
            if not await lock_user(session, user_id):
                raise UnknownItemError("The user has no saved item with that id.")
            item = await _owned_item(session, user_id, item_id, populate_existing=True)
            document = _document(snapshot.schema, item)
            previous = _open_value_at(snapshot.schema, document, corrected.path).wording
            set_value(document, corrected.path, normalized.value)
            raw_values = {
                **item.raw_values,
                corrected.path: {
                    RAW_FIELD: corrected.field,
                    RAW_WORDING: corrected.wording,
                    RAW_USER_SET: True,
                },
            }
            await vocabulary.apply_item_values(
                session, item, item_values(snapshot.schema, document), raw_values=raw_values
            )
            await self._record_provenance(session, item_id, corrected, normalized, previous)
        return previous

    async def _record_provenance(
        self,
        session: AsyncSession,
        item_id: uuid.UUID,
        corrected: OpenValue,
        normalized: NormalizedValue,
        previous: str,
    ) -> None:
        run = await vocabulary.normalize_run(session, item_id)
        if run is None:
            return
        output = run.output or {}
        provenance = output.get(PROVENANCE_VALUES)
        entries = dict(provenance) if isinstance(provenance, dict) else {}
        old_entry = entries.get(corrected.path)
        entries[corrected.path] = normalized.provenance(corrected.field)
        run.output = {**output, PROVENANCE_VALUES: entries}
        if previous != normalized.value and isinstance(old_entry, dict):
            await self._count_against_alias(session, old_entry.get(PROVENANCE_ALIAS_ID))

    async def _count_against_alias(self, session: AsyncSession, alias_id: object) -> None:
        if not isinstance(alias_id, str):
            return
        counted = await vocabulary.count_correction(session, uuid.UUID(alias_id))
        if counted is None:
            return
        if (
            counted.status == AliasStatus.VERIFIED
            and counted.correction_count >= self._review_min_corrections
        ):
            logger.warning(
                "vocabulary_alias_flagged_for_review",
                extra={"alias_id": alias_id, "corrections": counted.correction_count},
            )


async def _owned_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    populate_existing: bool = False,
) -> SavedItem:
    item = await session.get(SavedItem, item_id, populate_existing=populate_existing)
    # Another user's item is reported exactly like a missing one.
    if item is None or item.user_id != user_id:
        raise UnknownItemError("The user has no saved item with that id.")
    return item


def _document(schema: type[ExtractionModel], item: SavedItem) -> JsonDocument:
    return document_of_item(
        schema, summary=item.summary, category=item.category, tags=item.tags, fields=item.fields
    )


def _open_value_at(schema: type[ExtractionModel], document: JsonDocument, path: str) -> OpenValue:
    open_values = find_open_values(schema, document)
    for open_value in open_values:
        if open_value.path == path:
            return open_value
    paths = ", ".join(open_value.path for open_value in open_values) or "none"
    raise UnknownValuePathError(f"The item has no open value at '{path}'. Its open values: {paths}")
