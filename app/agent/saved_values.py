"""The values a search filter can take: those in one user's own saves, and the vocabulary's.

Filter normalization (ARCHITECTURE §3.4 and §3.5) maps what the user wrote to a
value that exists in **their** saves. These queries list those values. Every
query is limited to one user's items; the shared tables (`entity`,
`entity_availability`, `vocabulary`) are only read through that user's items,
or hold only gated, generic wording.

Nothing here lists real-world values in code: every value comes from the data.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Float, TextClause, bindparam, literal, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.search_query import CATEGORY_SLOT, COLLECTION_SLOT, Wording
from app.db.models import AliasStatus, Vocabulary, VocabularyAlias
from app.db.types import Vector
from app.types.base import FilterKind, FilterSource

# A JSON value, or each element of a JSON array, that is text. Lax mode treats a
# single value as an array of one, so one path covers both shapes.
_TEXT_ELEMENTS_PATH: Final = 'lax $[*] ? (@.type() == "string")'
# Each text in each list of a providers object, e.g. {"flatrate": ["Netflix"]}.
_PROVIDER_NAMES_PATH: Final = 'lax $.*[*] ? (@.type() == "string")'

_USER_COLLECTIONS: Final = text(
    """
    SELECT DISTINCT collection AS value FROM saved_item
    WHERE user_id = :user_id AND collection IS NOT NULL
    """
)

_USER_CATEGORIES: Final = text(
    """
    SELECT DISTINCT category AS value FROM saved_item
    WHERE user_id = :user_id AND category IS NOT NULL
    """
)

_USER_TAGS: Final = text(
    """
    SELECT DISTINCT tag.value FROM saved_item
    CROSS JOIN LATERAL unnest(saved_item.tags) AS tag(value)
    WHERE saved_item.user_id = :user_id
    """
)

_USER_FIELD_VALUES: Final = text(
    f"""
    SELECT DISTINCT found.value FROM (
        SELECT jsonb_path_query(saved_item.fields -> :key, '{_TEXT_ELEMENTS_PATH}') #>> '{{}}'
            AS value
        FROM saved_item
        WHERE saved_item.user_id = :user_id AND saved_item.type = :type
        UNION ALL
        SELECT jsonb_path_query(entity.data -> :key, '{_TEXT_ELEMENTS_PATH}') #>> '{{}}'
        FROM saved_item
        JOIN item_entity ON item_entity.saved_item_id = saved_item.id
        JOIN entity ON entity.id = item_entity.entity_id
        WHERE saved_item.user_id = :user_id AND saved_item.type = :type
    ) AS found
    WHERE found.value IS NOT NULL
    """
)

_USER_AREAS: Final = text(
    """
    SELECT DISTINCT level.value FROM saved_item
    JOIN item_entity ON item_entity.saved_item_id = saved_item.id
    JOIN entity ON entity.id = item_entity.entity_id
    CROSS JOIN LATERAL unnest(ARRAY[
        entity.country, entity.region, entity.city, entity.district, entity.neighbourhood
    ]) AS level(value)
    WHERE saved_item.user_id = :user_id AND saved_item.type = :type AND level.value IS NOT NULL
    """
)

_USER_PROVIDERS_IN_COUNTRY: Final = text(
    f"""
    SELECT DISTINCT
        jsonb_path_query(entity_availability.providers, '{_PROVIDER_NAMES_PATH}') #>> '{{}}'
            AS value
    FROM saved_item
    JOIN item_entity ON item_entity.saved_item_id = saved_item.id
    JOIN entity_availability
        ON entity_availability.entity_id = item_entity.entity_id
        AND entity_availability.country = :country
    WHERE saved_item.user_id = :user_id AND saved_item.type = :type
    """
)


@dataclass(frozen=True)
class ValueScope:
    """Whose saves, and which part of them, a wording's values come from.

    Attributes:
        user_id: The asking user.
        type: The content type the query is limited to, if any.
        country: The asking user's country, upper case, if known.
    """

    user_id: uuid.UUID
    type: str | None
    country: str | None


def comparison_form(wording: str) -> str:
    """Return wording in the form exact hits are compared in.

    It is the form the vocabulary stores values and aliases in (record 0005):
    trimmed, each run of white space one space, lower case.

    Args:
        wording: Any wording, in any language.

    Returns:
        The wording in comparison form.
    """
    return " ".join(wording.split()).lower()


async def saved_values(session: AsyncSession, scope: ValueScope, wording: Wording) -> list[str]:
    """List the values in the user's own saves that a wording could stand for.

    Args:
        session: The session to read in.
        scope: Whose saves to read.
        wording: The wording to find values for.

    Returns:
        The distinct values, as they are stored.
    """
    statement, parameters = _values_statement(scope, wording)
    rows = await session.execute(statement, parameters)
    return [value for value in rows.scalars() if value]


def _values_statement(scope: ValueScope, wording: Wording) -> tuple[TextClause, dict[str, object]]:
    parameters: dict[str, object] = {"user_id": scope.user_id}
    if wording.slot == COLLECTION_SLOT:
        return _USER_COLLECTIONS, parameters
    if wording.slot == CATEGORY_SLOT:
        return _USER_CATEGORIES, parameters
    search_filter = wording.search_filter
    if search_filter is None:
        return _USER_TAGS, parameters
    parameters["type"] = scope.type
    if search_filter.source is FilterSource.COUNTRY_AVAILABILITY:
        return _USER_PROVIDERS_IN_COUNTRY, {**parameters, "country": scope.country}
    if search_filter.kind is FilterKind.AREA:
        return _USER_AREAS, parameters
    return _USER_FIELD_VALUES, {**parameters, "key": search_filter.name}


async def vocabulary_exact_hit(session: AsyncSession, field: str, form: str) -> str | None:
    """Find the canonical value that wording equals, directly or through a verified alias.

    Candidate and rejected aliases never count.

    Args:
        session: The session to read in.
        field: The vocabulary field, e.g. `cuisine`.
        form: The wording in comparison form.

    Returns:
        The canonical value, or `None` if the wording is neither a value nor a
        verified alias of one.
    """
    direct = select(Vocabulary.value, literal(0).label("rank")).where(
        Vocabulary.field == field, Vocabulary.value == form
    )
    through_alias = (
        select(Vocabulary.value, literal(1).label("rank"))
        .join(VocabularyAlias, VocabularyAlias.vocabulary_id == Vocabulary.id)
        .where(
            Vocabulary.field == field,
            VocabularyAlias.alias == form,
            VocabularyAlias.status == AliasStatus.VERIFIED,
        )
    )
    hits = union_all(direct, through_alias).subquery()
    statement = select(hits.c.value).order_by(hits.c.rank, hits.c.value).limit(1)
    return (await session.execute(statement)).scalar_one_or_none()


async def rejected_values(session: AsyncSession, field: str, form: str) -> set[str]:
    """List the values that wording was rejected as an alias of.

    Args:
        session: The session to read in.
        field: The vocabulary field.
        form: The wording in comparison form.

    Returns:
        The canonical values the wording must never be matched to.
    """
    rows = await session.execute(
        select(Vocabulary.value)
        .join(VocabularyAlias, VocabularyAlias.vocabulary_id == Vocabulary.id)
        .where(
            Vocabulary.field == field,
            VocabularyAlias.alias == form,
            VocabularyAlias.status == AliasStatus.REJECTED,
        )
    )
    return set(rows.scalars())


async def nearest_vocabulary_values(
    session: AsyncSession,
    field: str,
    embedding: Sequence[float],
    *,
    among: Collection[str],
    limit: int,
) -> list[str]:
    """List the vocabulary values nearest in meaning to some wording, among the given ones.

    A near value is only a candidate for the decision model, never a match.

    Args:
        session: The session to read in.
        field: The vocabulary field.
        embedding: The wording's embedding.
        among: The only values that may be returned, e.g. those in the user's saves.
        limit: How many values at most.

    Returns:
        The nearest values first, by cosine distance.
    """
    if not among:
        return []
    distance = Vocabulary.embedding.op("<=>", return_type=Float)(
        bindparam("wording_embedding", list(embedding), type_=Vector())
    )
    rows = await session.execute(
        select(Vocabulary.value)
        .where(
            Vocabulary.field == field,
            Vocabulary.embedding.is_not(None),
            Vocabulary.value.in_(list(among)),
        )
        .order_by(distance)
        .limit(limit)
    )
    return list(rows.scalars())
