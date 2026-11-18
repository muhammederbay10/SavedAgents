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
    true,
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


@dataclass(frozen=True)
class FilterPredicates:
    """When one of a type's filters holds, on the item itself or on one linked entity.

    A list post links several entities. Every filter of a search must hold on
    the **same** entity, so two titles cannot each satisfy half of the filters.
    A value stored on the item itself (`saved_item.fields`) holds for all of
    its entities.

    Attributes:
        on_item: The filter holds by the item's own fields; `None` if it never can.
        on_entity: The filter holds by one linked entity, written on `Entity`;
            `None` if entities never decide it.
    """

    on_item: ColumnElement[bool] | None
    on_entity: ColumnElement[bool] | None


def filter_predicates(
    search_filter: SearchFilter, value: FilterValue, context: FilterContext
) -> FilterPredicates:
    """Build where one of a type's filters holds.

    Args:
        search_filter: The filter as the type declares it.
        value: The normalized value.
        context: The asking user and their country.

    Returns:
        The filter's predicates on the item and on one linked entity.
    """
    if search_filter.source is FilterSource.COUNTRY_AVAILABILITY:
        return FilterPredicates(None, _available_in_country(str(value), context.country))
    if search_filter.source is FilterSource.USER_STATUS:
        done = _done_by_user(context.user_id)
        return FilterPredicates(None, done if value else not_(done))
    if isinstance(value, Near):
        return near_predicates(value)
    if search_filter.kind is FilterKind.AREA:
        return area_predicates(str(value))
    if search_filter.kind is FilterKind.TEXT:
        return _text_predicates(search_filter.name, str(value))
    return _value_predicates(search_filter.name, value)


def area_predicates(area: str) -> FilterPredicates:
    """Hold for an entity whose address has the area at any level."""
    return FilterPredicates(None, or_(*(level == area for level in ADDRESS_LEVELS)))


def city_predicates(cities: Sequence[tuple[str | None, str]]) -> FilterPredicates:
    """Hold for an entity in one of the given cities.

    Args:
        cities: `(country, city)` pairs. The country keeps two cities with the
            same name in different countries apart.

    Returns:
        The predicates; they never hold when `cities` is empty.
    """
    if not cities:
        return FilterPredicates(None, false())
    return FilterPredicates(
        None,
        or_(
            *(
                and_(Entity.city == city, Entity.country.is_not_distinct_from(country))
                for country, city in cities
            )
        ),
    )


def near_predicates(near: Near) -> FilterPredicates:
    """Hold for an entity within a distance of a point."""
    return FilterPredicates(None, func.ST_DWithin(Entity.location, _point(near), near.radius_m))


def all_filters_hold(predicates: Sequence[FilterPredicates]) -> ColumnElement[bool]:
    """Keep the items on which every filter holds, all on the same linked entity.

    An item matches when its own fields satisfy every filter, or when one of
    its entities satisfies every filter its fields do not.

    Args:
        predicates: One per filter of the search.

    Returns:
        The condition on `saved_item`.
    """
    if not predicates:
        return true()
    per_entity = and_(*(_on_item_or_entity(predicate) for predicate in predicates))
    on_one_entity = (
        select(ItemEntity.saved_item_id)
        .join(Entity, Entity.id == ItemEntity.entity_id)
        .where(ItemEntity.saved_item_id == SavedItem.id, per_entity)
        .exists()
    )
    item_conditions = [predicate.on_item for predicate in predicates]
    if any(condition is None for condition in item_conditions):
        return on_one_entity
    return or_(
        and_(*(condition for condition in item_conditions if condition is not None)), on_one_entity
    )


def _on_item_or_entity(predicate: FilterPredicates) -> ColumnElement[bool]:
    parts = [part for part in (predicate.on_item, predicate.on_entity) if part is not None]
    return or_(*parts) if parts else false()


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


def _value_predicates(key: str, value: FilterValue) -> FilterPredicates:
    """The value under `key` in the item's fields or the entity's data, alone or in a list."""
    return FilterPredicates(
        or_(SavedItem.fields.contains({key: value}), SavedItem.fields.contains({key: [value]})),
        or_(Entity.data.contains({key: value}), Entity.data.contains({key: [value]})),
    )


def _text_predicates(key: str, value: str) -> FilterPredicates:
    pattern = f"%{_escape_like(value)}%"
    return FilterPredicates(
        SavedItem.fields[key].astext.ilike(pattern, escape=_LIKE_ESCAPE),
        Entity.data[key].astext.ilike(pattern, escape=_LIKE_ESCAPE),
    )


def _available_in_country(provider: str, country: str | None) -> ColumnElement[bool]:
    """The entity is offered by the provider in the country; another country's row never counts."""
    path = cast(literal(_PROVIDER_NAME_PATH), JSONPATH)
    variables = func.jsonb_build_object("name", provider)
    return (
        select(EntityAvailability.entity_id)
        .where(
            EntityAvailability.entity_id == Entity.id,
            EntityAvailability.country == country,
            func.jsonb_path_exists(EntityAvailability.providers, path, variables),
        )
        .exists()
    )


def _done_by_user(user_id: uuid.UUID) -> ColumnElement[bool]:
    """The user marked the entity as done (visited, watched, read)."""
    return (
        select(UserEntity.entity_id)
        .where(
            UserEntity.entity_id == Entity.id,
            UserEntity.user_id == user_id,
            UserEntity.status == EntityStatus.DONE,
        )
        .exists()
    )


def _point(near: Near) -> ColumnElement[object]:
    return cast(bindparam(None, near.point, type_=GeographyPoint()), GeographyPoint())


def _escape_like(value: str) -> str:
    for special in (_LIKE_ESCAPE, "%", "_"):
        value = value.replace(special, _LIKE_ESCAPE + special)
    return value
