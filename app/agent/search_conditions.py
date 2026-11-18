"""SQL conditions on `saved_item` for each part of a search, and the orders results come in.

Each condition is checked on one user's items. Conditions on entities look at
the entities linked to the item, so a list post matches when any of its
places or movies does. Where a filter reads its values is declared by the
content type (`FilterSource`); nothing here names a type or a filter.

Values come normalized (`filter_normalization`), so they are compared exactly,
the way they are stored, and the JSON indexes can be used.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Float,
    and_,
    bindparam,
    cast,
    false,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONPATH, TSQUERY

from app.agent.search_query import FilterValue, Near
from app.db.models import (
    FULL_TEXT_SEARCH_CONFIG,
    Entity,
    EntityAvailability,
    EntityStatus,
    ItemEntity,
    ItemStatus,
    SavedItem,
    UserEntity,
    saved_item_search_vector,
)
from app.db.types import GeographyPoint, Vector
from app.types.base import FilterKind, FilterSource, SearchFilter

# The address levels of a place, from the widest to the narrowest. An area filter
# matches any of them (record 0005).
ADDRESS_LEVELS: Final = (
    Entity.country,
    Entity.region,
    Entity.city,
    Entity.district,
    Entity.neighbourhood,
)
# The levels L2 widens an area from, to the city they are in.
WIDENED_LEVELS: Final = (Entity.district, Entity.neighbourhood)

# Matches a provider name in any list of a providers object, e.g. {"flatrate": ["Netflix"]}.
_PROVIDER_NAME_PATH: Final = "lax $.*[*] ? (@ == $name)"
_LIKE_ESCAPE: Final = "\\"


def owned_by(user_id: uuid.UUID) -> ColumnElement[bool]:
    """Keep only the items of one user."""
    return SavedItem.user_id == user_id


def fully_indexed() -> ColumnElement[bool]:
    """Keep only items the pipeline has finished."""
    return SavedItem.status == ItemStatus.DONE


def not_fully_indexed() -> ColumnElement[bool]:
    """Keep only items the pipeline has not finished: pending, enriching, waiting or failed."""
    return SavedItem.status != ItemStatus.DONE


def in_collection(collection: str) -> ColumnElement[bool]:
    """Keep the items in one of the user's collections."""
    return SavedItem.collection == collection


def of_type(type_name: str) -> ColumnElement[bool]:
    """Keep the items of one built-in content type."""
    return SavedItem.type == type_name


def in_category(category: str) -> ColumnElement[bool]:
    """Keep the items with one open category."""
    return SavedItem.category == category


def tagged(tag: str) -> ColumnElement[bool]:
    """Keep the items with one tag."""
    return SavedItem.tags.contains([tag])


def from_platform(platform: str) -> ColumnElement[bool]:
    """Keep the items from one platform."""
    return SavedItem.platform == platform


def saved_after(moment: datetime) -> ColumnElement[bool]:
    """Keep the items saved after a moment."""
    return SavedItem.saved_at > moment


@dataclass(frozen=True)
class FilterContext:
    """What a type filter's condition needs besides its value.

    Attributes:
        user_id: The asking user, for their own statuses.
        country: The asking user's country, upper case, for availability.
    """

    user_id: uuid.UUID
    country: str | None


def filter_condition(
    search_filter: SearchFilter, value: FilterValue, context: FilterContext
) -> ColumnElement[bool]:
    """Build the condition of one of a type's filters.

    Args:
        search_filter: The filter as the type declares it.
        value: The normalized value.
        context: The asking user and their country.

    Returns:
        The condition on `saved_item`.
    """
    if search_filter.source is FilterSource.COUNTRY_AVAILABILITY:
        return _available_in_country(str(value), context.country)
    if search_filter.source is FilterSource.USER_STATUS:
        return _done_by_user(context.user_id) if value else not_(_done_by_user(context.user_id))
    if isinstance(value, Near):
        return within(value)
    if search_filter.kind is FilterKind.AREA:
        return in_area(str(value))
    if search_filter.kind is FilterKind.TEXT:
        return _mentions_text(search_filter.name, str(value))
    return _has_value(search_filter.name, value)


def in_area(area: str) -> ColumnElement[bool]:
    """Keep the items with an entity whose address has the area at any level."""
    return _has_entity(or_(*(level == area for level in ADDRESS_LEVELS)))


def in_any_city(cities: Sequence[tuple[str | None, str]]) -> ColumnElement[bool]:
    """Keep the items with an entity in one of the given cities.

    Args:
        cities: `(country, city)` pairs. The country keeps two cities with the
            same name in different countries apart.

    Returns:
        The condition on `saved_item`.
    """
    if not cities:
        return false()
    return _has_entity(
        or_(
            *(
                and_(Entity.city == city, Entity.country.is_not_distinct_from(country))
                for country, city in cities
            )
        )
    )


def within(near: Near) -> ColumnElement[bool]:
    """Keep the items with an entity within a distance of a point."""
    return _has_entity(func.ST_DWithin(Entity.location, _point(near), near.radius_m))


def distance_from(near: Near) -> ColumnElement[float]:
    """The distance in metres from a point to the item's nearest entity; null if none has one."""
    return (
        select(func.min(func.ST_Distance(Entity.location, _point(near))))
        .join(ItemEntity, ItemEntity.entity_id == Entity.id)
        .where(ItemEntity.saved_item_id == SavedItem.id)
        .scalar_subquery()
    )


def vector_distance(embedding: Sequence[float]) -> ColumnElement[float]:
    """The cosine distance from a query embedding to the item's embedding."""
    query_vector = bindparam("query_embedding", list(embedding), type_=Vector())
    return SavedItem.embedding.op("<=>", return_type=Float)(query_vector)


def keyword_query(words: Sequence[str]) -> ColumnElement[str]:
    """A full-text query that matches any of the words, in any language.

    Args:
        words: The words. At least one.

    Returns:
        A `tsquery` that matches text with any of the words.
    """
    config: ColumnElement[str] = literal_column(f"'{FULL_TEXT_SEARCH_CONFIG}'::regconfig")
    queries: list[ColumnElement[str]] = [
        func.plainto_tsquery(config, word, type_=TSQUERY) for word in words
    ]
    combined = queries[0]
    for query in queries[1:]:
        combined = combined.op("||", return_type=TSQUERY)(query)
    return combined


def matches_keywords(query: ColumnElement[str]) -> ColumnElement[bool]:
    """Keep the items whose caption, summary or transcript has a word of the query."""
    return saved_item_search_vector().op("@@", return_type=Boolean)(query)


def keyword_rank(query: ColumnElement[str]) -> ColumnElement[float]:
    """How well the item's caption, summary and transcript match the query."""
    return func.ts_rank(saved_item_search_vector(), query, type_=Float)


def _has_entity(condition: ColumnElement[bool]) -> ColumnElement[bool]:
    return (
        select(ItemEntity.saved_item_id)
        .join(Entity, Entity.id == ItemEntity.entity_id)
        .where(ItemEntity.saved_item_id == SavedItem.id, condition)
        .exists()
    )


def _has_value(key: str, value: FilterValue) -> ColumnElement[bool]:
    """The value under `key` in the item's fields or an entity's data, alone or in a list."""
    return or_(
        SavedItem.fields.contains({key: value}),
        SavedItem.fields.contains({key: [value]}),
        _has_entity(or_(Entity.data.contains({key: value}), Entity.data.contains({key: [value]}))),
    )


def _mentions_text(key: str, value: str) -> ColumnElement[bool]:
    pattern = f"%{_escape_like(value)}%"
    return or_(
        SavedItem.fields[key].astext.ilike(pattern, escape=_LIKE_ESCAPE),
        _has_entity(Entity.data[key].astext.ilike(pattern, escape=_LIKE_ESCAPE)),
    )


def _available_in_country(provider: str, country: str | None) -> ColumnElement[bool]:
    path = cast(literal(_PROVIDER_NAME_PATH), JSONPATH)
    variables = func.jsonb_build_object("name", provider)
    return (
        select(ItemEntity.saved_item_id)
        .join(
            EntityAvailability,
            and_(
                EntityAvailability.entity_id == ItemEntity.entity_id,
                EntityAvailability.country == country,
            ),
        )
        .where(
            ItemEntity.saved_item_id == SavedItem.id,
            func.jsonb_path_exists(EntityAvailability.providers, path, variables),
        )
        .exists()
    )


def _done_by_user(user_id: uuid.UUID) -> ColumnElement[bool]:
    return (
        select(ItemEntity.saved_item_id)
        .join(
            UserEntity,
            and_(
                UserEntity.entity_id == ItemEntity.entity_id,
                UserEntity.user_id == user_id,
                UserEntity.status == EntityStatus.DONE,
            ),
        )
        .where(ItemEntity.saved_item_id == SavedItem.id)
        .exists()
    )


def _point(near: Near) -> ColumnElement[object]:
    return cast(bindparam(None, near.point, type_=GeographyPoint()), GeographyPoint())


def _escape_like(value: str) -> str:
    for special in (_LIKE_ESCAPE, "%", "_"):
        value = value.replace(special, _LIKE_ESCAPE + special)
    return value
