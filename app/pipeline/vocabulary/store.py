"""Database work for the shared vocabulary (`docs/ARCHITECTURE.md` §3.4, §4).

Every function runs inside the caller's transaction and never commits. The
vocabulary has no link to a user; which items matched through an alias is read
from their normalize step in `enrichment_run` (decision record 0007).

Values and aliases are stored in their **canonical form**: trimmed, with runs
of white space made one space, and lower-cased (`canonical_form`). An exact
hit compares the canonical form of new wording with them.
"""

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Float, bindparam, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AliasStatus, EnrichmentRun, SavedItem, Vocabulary, VocabularyAlias
from app.db.types import Vector
from app.pipeline.recovery.runs import RunStatus, Step
from app.pipeline.vocabulary.open_values import ItemValues, vocabulary_pairs

NORMALIZE_STEP: Final = Step.NORMALIZE.value
DONE_STATUS: Final = RunStatus.DONE.value

# The users who matched wording through one alias with enough confidence, read
# from the provenance the normalize step keeps per item (`values.<path>`).
_CONFIDENT_USERS = text(
    """
    SELECT count(DISTINCT saved_item.user_id)
    FROM enrichment_run
    JOIN saved_item ON saved_item.id = enrichment_run.saved_item_id
    CROSS JOIN LATERAL jsonb_each(enrichment_run.output -> 'values') AS provenance(path, entry)
    WHERE enrichment_run.step = :step
      AND enrichment_run.status = :status
      AND provenance.entry ->> 'alias_id' = :alias_id
      AND CAST(provenance.entry ->> 'probability' AS double precision) >= :min_probability
    """
)

_ITEMS_MATCHED_THROUGH = text(
    """
    SELECT DISTINCT enrichment_run.saved_item_id
    FROM enrichment_run
    CROSS JOIN LATERAL jsonb_each(enrichment_run.output -> 'values') AS provenance(path, entry)
    WHERE enrichment_run.step = :step
      AND enrichment_run.status = :status
      AND provenance.entry ->> 'alias_id' = :alias_id
    """
)


def canonical_form(wording: str) -> str:
    """Return wording in the form values and aliases are stored and compared in.

    Args:
        wording: Any wording, in any language.

    Returns:
        The wording trimmed, with each run of white space made one space, and
        lower-cased.
    """
    return " ".join(wording.split()).lower()


@dataclass(frozen=True)
class ExactHit:
    """A canonical value that wording equals, directly or through a verified alias.

    Attributes:
        vocabulary_id: The value's id.
        value: The canonical value.
        alias_id: The verified alias the wording equals; `None` when it equals
            the value itself.
    """

    vocabulary_id: uuid.UUID
    value: str
    alias_id: uuid.UUID | None


@dataclass(frozen=True)
class Candidate:
    """An existing value near some wording, offered to the decision model.

    Attributes:
        vocabulary_id: The value's id.
        value: The canonical value.
    """

    vocabulary_id: uuid.UUID
    value: str


async def find_exact(session: AsyncSession, field: str, form: str) -> ExactHit | None:
    """Find the canonical value or verified alias equal to wording, in one field.

    Candidate and rejected aliases never count: only verified ones do.

    Args:
        session: The session to work in.
        field: The vocabulary field, e.g. `cuisine`.
        form: The wording's canonical form.

    Returns:
        The value it equals, or `None`.
    """
    value = await session.execute(
        select(Vocabulary.id, Vocabulary.value).where(
            Vocabulary.field == field, Vocabulary.value == form
        )
    )
    value_row = value.first()
    if value_row is not None:
        return ExactHit(vocabulary_id=value_row.id, value=value_row.value, alias_id=None)
    alias = await session.execute(
        select(VocabularyAlias.id, Vocabulary.id.label("vocabulary_id"), Vocabulary.value)
        .join(Vocabulary, Vocabulary.id == VocabularyAlias.vocabulary_id)
        .where(
            Vocabulary.field == field,
            VocabularyAlias.alias == form,
            VocabularyAlias.status == AliasStatus.VERIFIED,
        )
        .order_by(VocabularyAlias.created_at)
        .limit(1)
    )
    alias_row = alias.first()
    if alias_row is None:
        return None
    return ExactHit(
        vocabulary_id=alias_row.vocabulary_id, value=alias_row.value, alias_id=alias_row.id
    )


async def nearest_values(
    session: AsyncSession,
    field: str,
    embedding: list[float],
    *,
    limit: int,
    excluded_ids: Collection[uuid.UUID],
) -> list[Candidate]:
    """Find the existing values of a field whose embeddings are nearest to wording's.

    Args:
        session: The session to work in.
        field: The vocabulary field.
        embedding: The wording's embedding.
        limit: How many values at most.
        excluded_ids: Values never to offer, such as those the wording was
            rejected as an alias of.

    Returns:
        The nearest values first, by cosine distance.
    """
    distance = Vocabulary.embedding.op("<=>", return_type=Float)(
        bindparam("query_embedding", embedding, type_=Vector())
    )
    statement = (
        select(Vocabulary.id, Vocabulary.value)
        .where(Vocabulary.field == field, Vocabulary.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    )
    if excluded_ids:
        statement = statement.where(Vocabulary.id.not_in(list(excluded_ids)))
    rows = await session.execute(statement)
    return [Candidate(vocabulary_id=row.id, value=row.value) for row in rows]


async def rejected_value_ids(session: AsyncSession, field: str, form: str) -> set[uuid.UUID]:
    """Return the values that wording was rejected as an alias of, in one field.

    Args:
        session: The session to work in.
        field: The vocabulary field.
        form: The wording's canonical form.

    Returns:
        Their ids.
    """
    rows = await session.execute(
        select(VocabularyAlias.vocabulary_id)
        .join(Vocabulary, Vocabulary.id == VocabularyAlias.vocabulary_id)
        .where(
            Vocabulary.field == field,
            VocabularyAlias.alias == form,
            VocabularyAlias.status == AliasStatus.REJECTED,
        )
    )
    return set(rows.scalars())


async def add_value(
    session: AsyncSession, field: str, value: str, embedding: list[float]
) -> uuid.UUID:
    """Add a canonical value, or find it if another worker just added it.

    Only call this for wording that passed the privacy gate.

    Args:
        session: The session to work in.
        field: The vocabulary field.
        value: The canonical value.
        embedding: Its embedding.

    Returns:
        The value's id.
    """
    await session.execute(
        insert(Vocabulary)
        .values(field=field, value=value, embedding=embedding)
        .on_conflict_do_nothing(index_elements=[Vocabulary.field, Vocabulary.value])
    )
    found = await session.execute(
        select(Vocabulary.id).where(Vocabulary.field == field, Vocabulary.value == value)
    )
    return found.scalar_one()


async def record_candidate_alias(
    session: AsyncSession, vocabulary_id: uuid.UUID, alias: str, probability: float | None
) -> uuid.UUID:
    """Store wording as a candidate alias of a value, or update the one that exists.

    A new alias is a candidate. An existing alias keeps its status and its
    best probability. Only call this for wording that passed the privacy gate.

    Args:
        session: The session to work in.
        vocabulary_id: The value the wording was matched to.
        alias: The wording's canonical form.
        probability: The decision model's probability for the match.

    Returns:
        The alias's id.
    """
    new_alias = insert(VocabularyAlias).values(
        vocabulary_id=vocabulary_id, alias=alias, probability=probability
    )
    statement = new_alias.on_conflict_do_update(
        index_elements=[VocabularyAlias.vocabulary_id, VocabularyAlias.alias],
        set_={
            "probability": func.greatest(
                VocabularyAlias.probability, new_alias.excluded.probability
            )
        },
    ).returning(VocabularyAlias.id)
    alias_id: uuid.UUID = (await session.execute(statement)).scalar_one()
    return alias_id


async def change_item_counts(
    session: AsyncSession, pairs: Iterable[tuple[str, str]], change: int
) -> None:
    """Raise or lower `item_count` of the values an item started or stopped using.

    Pairs that match no value change nothing. A count never goes below zero.

    Args:
        session: The session to work in.
        pairs: `(vocabulary field, value)` pairs, each once per item.
        change: `1` or `-1`.
    """
    pair_list = sorted(set(pairs))
    if not pair_list:
        return
    await session.execute(
        update(Vocabulary)
        .where(tuple_(Vocabulary.field, Vocabulary.value).in_(pair_list))
        .values(item_count=func.greatest(Vocabulary.item_count + change, 0))
    )


async def confident_user_count(
    session: AsyncSession, alias_id: uuid.UUID, min_probability: float
) -> int:
    """Count the users whose items matched through an alias with enough confidence.

    Args:
        session: The session to work in.
        alias_id: The alias.
        min_probability: The lowest match probability that counts.

    Returns:
        How many different users.
    """
    result = await session.execute(
        _CONFIDENT_USERS,
        {
            "step": NORMALIZE_STEP,
            "status": DONE_STATUS,
            "alias_id": str(alias_id),
            "min_probability": min_probability,
        },
    )
    return int(result.scalar_one())


async def verify_alias(session: AsyncSession, alias_id: uuid.UUID) -> bool:
    """Mark a candidate alias verified, so it counts for exact hits.

    A candidate that a user's correction counted against is never verified
    (ARCHITECTURE §3.4).

    Args:
        session: The session to work in.
        alias_id: The alias.

    Returns:
        Whether it changed; a verified, rejected or corrected alias stays as it is.
    """
    result = await session.execute(
        update(VocabularyAlias)
        .where(
            VocabularyAlias.id == alias_id,
            VocabularyAlias.status == AliasStatus.CANDIDATE,
            VocabularyAlias.correction_count == 0,
        )
        .values(status=AliasStatus.VERIFIED)
        .returning(VocabularyAlias.id)
    )
    return result.first() is not None


@dataclass(frozen=True)
class CountedCorrection:
    """An alias after a user's correction was counted against it.

    Attributes:
        status: The alias's status, e.g. `verified`.
        correction_count: How many corrections it has now.
    """

    status: str
    correction_count: int


async def count_correction(session: AsyncSession, alias_id: uuid.UUID) -> CountedCorrection | None:
    """Count one user correction against the alias that produced the corrected value.

    Args:
        session: The session to work in.
        alias_id: The alias.

    Returns:
        The alias's status and new count, or `None` if the alias is gone.
    """
    result = await session.execute(
        update(VocabularyAlias)
        .where(VocabularyAlias.id == alias_id)
        .values(correction_count=VocabularyAlias.correction_count + 1)
        .returning(VocabularyAlias.status, VocabularyAlias.correction_count)
    )
    row = result.first()
    if row is None:
        return None
    return CountedCorrection(status=row.status, correction_count=row.correction_count)


async def reject_alias(session: AsyncSession, alias_id: uuid.UUID) -> bool:
    """Mark an alias rejected, so it is never matched or used again.

    Args:
        session: The session to work in.
        alias_id: The alias.

    Returns:
        Whether the alias exists.
    """
    result = await session.execute(
        update(VocabularyAlias)
        .where(VocabularyAlias.id == alias_id)
        .values(status=AliasStatus.REJECTED)
        .returning(VocabularyAlias.id)
    )
    return result.first() is not None


async def items_matched_through(session: AsyncSession, alias_id: uuid.UUID) -> list[uuid.UUID]:
    """Find the items whose normalize step used an alias.

    Args:
        session: The session to work in.
        alias_id: The alias.

    Returns:
        The items' ids.
    """
    result = await session.execute(
        _ITEMS_MATCHED_THROUGH,
        {"step": NORMALIZE_STEP, "status": DONE_STATUS, "alias_id": str(alias_id)},
    )
    return list(result.scalars())


async def normalize_run(session: AsyncSession, saved_item_id: uuid.UUID) -> EnrichmentRun | None:
    """Return an item's finished normalize step.

    Args:
        session: The session to work in.
        saved_item_id: The item.

    Returns:
        The step's row, or `None` if the item was never normalized.
    """
    result = await session.execute(
        select(EnrichmentRun).where(
            EnrichmentRun.saved_item_id == saved_item_id,
            EnrichmentRun.step == NORMALIZE_STEP,
            EnrichmentRun.status == DONE_STATUS,
        )
    )
    return result.scalars().first()


async def owner_of(session: AsyncSession, saved_item_id: uuid.UUID) -> uuid.UUID | None:
    """Return the user who owns an item.

    Args:
        session: The session to work in.
        saved_item_id: The item.

    Returns:
        The owner's id, or `None` if the item is gone.
    """
    result = await session.execute(select(SavedItem.user_id).where(SavedItem.id == saved_item_id))
    return result.scalar_one_or_none()


async def apply_item_values(
    session: AsyncSession,
    item: SavedItem,
    values: ItemValues,
    *,
    raw_values: dict[str, object] | None = None,
) -> None:
    """Put normalized values on an item, and move the vocabulary's counts with them.

    Each value the item stops using loses one from its `item_count`, and each
    value it starts using gains one, by the rule `delete_user_data` lowers them
    with (decision record 0007).

    Args:
        session: The session to work in. The caller holds the user's row lock.
        item: The item, loaded in this session.
        values: The new summary, category, tags and `fields`.
        raw_values: The item's own wording, when it changes; `None` keeps it.
    """
    before = vocabulary_pairs(
        category=item.category, subtype=item.subtype, tags=item.tags, fields=item.fields
    )
    item.summary = values.summary
    item.category = values.category
    item.tags = values.tags
    item.fields = values.fields
    if raw_values is not None:
        item.raw_values = raw_values
    after = vocabulary_pairs(
        category=item.category, subtype=item.subtype, tags=item.tags, fields=item.fields
    )
    await change_item_counts(session, before - after, -1)
    await change_item_counts(session, after - before, 1)
