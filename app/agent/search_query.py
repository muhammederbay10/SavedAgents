"""What a search over one user's saves asks for, checked against the content types.

The response agent's `search_items` and `near` tools (ARCHITECTURE §3.5) turn
their arguments into a `SearchQuery`. `plan_search` checks it against the
content-type registry, so search never names a type or a filter itself.

    query = SearchQuery(user_id=user.id, country="US", type="movie",
                        filters={"genre": "horror", "streaming": "netflx"})
    plan = plan_search(query, CONTENT_TYPES)
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from app.db.models import CATEGORY_VOCABULARY_FIELD, TAG_VOCABULARY_FIELD
from app.db.types import GeoPoint
from app.types.base import ContentType, FilterKind, FilterSource, SearchFilter
from app.types.registry import ContentTypeRegistry


class SearchQueryError(ValueError):
    """A search asks for something that cannot be searched for.

    The message is written so the response agent can correct its arguments.
    """


class CountryRequiredError(SearchQueryError):
    """A filter depends on the user's country, and the search was not given one.

    The response agent asks the user once and stores the answer in their profile.
    """


class UnsupportedFilterError(SearchQueryError):
    """A content type declares this filter, but search cannot check it yet."""


@dataclass(frozen=True)
class Near:
    """A point and a distance around it, for a `LOCATION` filter.

    Attributes:
        point: Where the user is, or the point they named.
        radius_m: How far from the point a place may be, in metres.
    """

    point: GeoPoint
    radius_m: float

    def __post_init__(self) -> None:
        """Reject a distance that is not a positive number of metres.

        Raises:
            SearchQueryError: If `radius_m` is not above zero.
        """
        if not self.radius_m > 0:
            raise SearchQueryError("A near filter needs a radius above 0 metres")


FilterValue = str | int | bool | Near


@dataclass(frozen=True)
class SearchQuery:
    """One search over one user's saves.

    Attributes:
        user_id: Whose saves are searched. Nothing of another user is ever read.
        country: The asking user's country as an ISO 3166-1 alpha-2 code, e.g.
            `US`. Filters on where a title can be streamed need it; `None` if
            it is not known yet.
        collection: The user's collection, e.g. "Food", in the user's own words.
        type: A built-in content type, e.g. `place`.
        category: An open category, e.g. "car review".
        tags: Tags every item must have.
        filters: The type's own filters by name, e.g. `{"area": "Beşiktaş"}`.
            They need `type`.
        text: What to rank by meaning, e.g. "cozy date-night place".
        platform: Only items from this platform, e.g. `instagram`.
        saved_after: Only items saved after this moment.
        limit: The most items to return; `None` for the configured maximum.
    """

    user_id: uuid.UUID
    country: str | None = None
    collection: str | None = None
    type: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = ()
    filters: Mapping[str, FilterValue] = field(default_factory=dict)
    text: str | None = None
    platform: str | None = None
    saved_after: datetime | None = None
    limit: int | None = None


@dataclass(frozen=True)
class Wording:
    """A filter value as the user wrote it, which normalization maps to a saved value.

    Attributes:
        slot: Where the wording sits in the query, e.g. `collection`,
            `tags.0` or `filters.area`.
        wording: What the user wrote.
        vocabulary: The vocabulary field whose canonical values and verified
            aliases count as exact hits; `None` when the values are not in the
            vocabulary, such as collections, areas and streaming platforms.
        search_filter: The type's filter the wording is for; `None` for
            collection, category and tags.
    """

    slot: str
    wording: str = field(repr=False)
    vocabulary: str | None
    search_filter: SearchFilter | None


@dataclass(frozen=True)
class TypedFilter:
    """One of the type's filters with the value the query gives it.

    Attributes:
        search_filter: The filter as the type declares it.
        value: The value from the query. A `Wording` value is normalized first.
        item_wide: Whether the type's extraction schema has a top-level field
            of the filter's name, such as a study post's `subject`. Its value
            describes the whole item. Any other value in `saved_item.fields`
            under that name, such as the cuisines of a list of places, is only
            a list of what its mentions say, and belongs to one mention each.
    """

    search_filter: SearchFilter
    value: FilterValue = field(repr=False)
    item_wide: bool = False


@dataclass(frozen=True)
class SearchPlan:
    """A query checked against the content types, ready for normalization.

    Attributes:
        query: The query as it was asked.
        content_type: The built-in type it is limited to, if any.
        wordings: Every value that is normalized against the user's saves.
        filters: Every type filter of the query, including those with a wording.
    """

    query: SearchQuery
    content_type: ContentType | None
    wordings: tuple[Wording, ...]
    filters: tuple[TypedFilter, ...]

    @property
    def uses_country(self) -> bool:
        """Return whether a filter depends on the asking user's country."""
        return any(
            typed.search_filter.source is FilterSource.COUNTRY_AVAILABILITY
            for typed in self.filters
        )


COLLECTION_SLOT = "collection"
CATEGORY_SLOT = "category"
TAG_SLOT_PREFIX = "tags."
FILTER_SLOT_PREFIX = "filters."

# A filter of these kinds takes a value the user wrote in their own words, so it
# is normalized against the values in their saves before it is applied.
_NORMALIZED_KINDS = frozenset({FilterKind.VALUE, FilterKind.AREA})

_VALUE_TYPES: dict[FilterKind, tuple[type, ...]] = {
    FilterKind.VALUE: (str,),
    FilterKind.AREA: (str,),
    FilterKind.TEXT: (str,),
    FilterKind.LOCATION: (Near,),
    FilterKind.BOOLEAN: (bool,),
    FilterKind.INTEGER: (int,),
}


def plan_search(query: SearchQuery, registry: ContentTypeRegistry) -> SearchPlan:
    """Check a query against the content types and list what must be normalized.

    Args:
        query: The search to check.
        registry: The built-in content types.

    Returns:
        The checked query.

    Raises:
        SearchQueryError: If the type is unknown, a filter is given without a
            type or is not one of the type's filters, a value has the wrong
            kind, or a text value is blank.
        CountryRequiredError: If a filter depends on the country and none is given.
        UnsupportedFilterError: If search cannot check one of the filters yet.
    """
    _require_valid_limit(query.limit)
    content_type = _content_type_of(query, registry)
    filters = _typed_filters(query, content_type)
    wordings = (*_scope_wordings(query), *_filter_wordings(filters))
    plan = SearchPlan(query=query, content_type=content_type, wordings=wordings, filters=filters)
    if plan.uses_country and not (query.country and query.country.strip()):
        raise CountryRequiredError(
            "This search filters on streaming platforms, which differ by country, "
            "and the user's country is not known. Ask the user for it once."
        )
    return plan


def _require_valid_limit(limit: int | None) -> None:
    if limit is not None and limit < 1:
        raise SearchQueryError("A search limit must be at least 1")


def _content_type_of(query: SearchQuery, registry: ContentTypeRegistry) -> ContentType | None:
    if query.type is None:
        if query.filters:
            raise SearchQueryError("Filters belong to a content type, so give the type as well")
        return None
    known = {content_type.name: content_type for content_type in registry.all()}
    if query.type not in known:
        raise SearchQueryError(
            f"Unknown content type '{query.type}'. Known types: {', '.join(known)}"
        )
    return known[query.type]


def _typed_filters(query: SearchQuery, content_type: ContentType | None) -> tuple[TypedFilter, ...]:
    if content_type is None:
        return ()
    declared = {search_filter.name: search_filter for search_filter in content_type.search_filters}
    typed: list[TypedFilter] = []
    for name, value in query.filters.items():
        search_filter = declared.get(name)
        if search_filter is None:
            raise SearchQueryError(
                f"'{name}' is not a filter of the type '{content_type.name}'. "
                f"Its filters: {', '.join(declared)}"
            )
        _require_value_fits(search_filter, value)
        if search_filter.source is FilterSource.OPENING_HOURS:
            raise UnsupportedFilterError(
                f"Search cannot check '{name}' yet: it needs the opening hours the place "
                "resolver stores"
            )
        typed.append(
            TypedFilter(
                search_filter=search_filter,
                value=value,
                item_wide=name in content_type.extraction_schema.model_fields,
            )
        )
    return tuple(typed)


def _require_value_fits(search_filter: SearchFilter, value: FilterValue) -> None:
    expected = _VALUE_TYPES[search_filter.kind]
    # `bool` is a subclass of `int`, so a boolean must not pass as a whole number.
    is_bool_for_integer = search_filter.kind is FilterKind.INTEGER and isinstance(value, bool)
    if not isinstance(value, expected) or is_bool_for_integer:
        raise SearchQueryError(
            f"The filter '{search_filter.name}' takes a {search_filter.kind} value"
        )
    if isinstance(value, str) and not value.strip():
        raise SearchQueryError(f"The filter '{search_filter.name}' needs a value that is not blank")


def _scope_wordings(query: SearchQuery) -> list[Wording]:
    wordings: list[Wording] = []
    if query.collection is not None:
        wordings.append(_wording(COLLECTION_SLOT, query.collection, vocabulary=None))
    if query.category is not None:
        wordings.append(_wording(CATEGORY_SLOT, query.category, CATEGORY_VOCABULARY_FIELD))
    wordings.extend(
        _wording(f"{TAG_SLOT_PREFIX}{index}", tag, TAG_VOCABULARY_FIELD)
        for index, tag in enumerate(query.tags)
    )
    return wordings


def _wording(slot: str, wording: str, vocabulary: str | None) -> Wording:
    if not wording.strip():
        raise SearchQueryError(f"The search's {slot} must not be blank")
    return Wording(slot=slot, wording=wording, vocabulary=vocabulary, search_filter=None)


def _filter_wordings(filters: tuple[TypedFilter, ...]) -> list[Wording]:
    return [
        Wording(
            slot=f"{FILTER_SLOT_PREFIX}{typed.search_filter.name}",
            wording=typed.value,
            vocabulary=typed.search_filter.vocabulary,
            search_filter=typed.search_filter,
        )
        for typed in filters
        if typed.search_filter.kind in _NORMALIZED_KINDS and isinstance(typed.value, str)
    ]
