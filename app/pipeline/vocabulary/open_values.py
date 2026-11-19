"""Finding an extraction's open values, and turning an extraction into a saved item's columns.

An open value is a schema field marked `Vocabulary(...)` (decision record 0006):
the category, each tag, and fields such as a study post's subject or a place's
cuisine. Each one has a **path** in the extraction, written with dots:
`category`, `tags.0`, `subject`, `entities.1.cuisine`. The path names the value
in `saved_item.raw_values` and in the normalize step's provenance.

The schemas are read generically: nothing here names a type or a field, except
the three fields every extraction shares (summary, category, tags), which are
`saved_item` columns.

**Where values go on the item.** `summary`, `category` and `tags` are columns.
Every other field of the extraction goes into `saved_item.fields` under its
name. Open values found inside nested objects, such as the cuisine of each
place in a list post, are also listed once at the top of `fields` under their
vocabulary field (`"cuisine": ["korean barbecue", "specialty coffee"]`). That
is where `delete_user_data` counts an item's values (decision record 0007), and
where search can filter on them.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import NoneType, UnionType
from typing import Final, Union, get_args, get_origin

from app.db.models import CATEGORY_VOCABULARY_FIELD, SUBTYPE_VOCABULARY_FIELD, TAG_VOCABULARY_FIELD
from app.types.base import Extraction, ExtractionModel, vocabulary_fields

PATH_SEPARATOR: Final = "."
_SUMMARY: Final = "summary"
_CATEGORY: Final = "category"
_TAGS: Final = "tags"
# The fields every extraction has; they are columns of `saved_item`, not `fields`.
SHARED_FIELDS: Final = frozenset(Extraction.model_fields)

JsonDocument = dict[str, object]


@dataclass(frozen=True)
class OpenValue:
    """One open value of an extraction, in the text model's own words.

    Attributes:
        path: Where it is in the extraction, e.g. `tags.0` or `entities.1.cuisine`.
        field: The vocabulary field it belongs to, e.g. `tag` or `cuisine`.
        wording: The value as the text model wrote it.
        nested: Whether it sits inside a nested object, such as one entity.
    """

    path: str
    field: str
    wording: str
    nested: bool


@dataclass(frozen=True)
class ItemValues:
    """What an extraction puts on a saved item.

    Attributes:
        summary: `saved_item.summary`.
        category: `saved_item.category`.
        tags: `saved_item.tags`.
        fields: `saved_item.fields`.
    """

    summary: str
    category: str
    tags: list[str]
    fields: JsonDocument


def find_open_values(
    schema: type[ExtractionModel], document: Mapping[str, object]
) -> list[OpenValue]:
    """List every open value of an extraction, in the order of the schema.

    Args:
        schema: The schema the extraction was checked against.
        document: The extraction as JSON.

    Returns:
        Each open value with its path. Null values and empty lists have none.
    """
    return list(_walk(schema, document, prefix=(), nested=False))


def set_value(document: JsonDocument, path: str, value: str) -> None:
    """Replace the value at a path of an extraction.

    Args:
        document: The extraction as JSON. It is changed in place.
        path: A path that `find_open_values` returned for it.
        value: The new value.

    Raises:
        KeyError: If the path doesn't exist in the document.
    """
    *parents, last = path.split(PATH_SEPARATOR)
    container: object = document
    for part in parents:
        container = _child(container, part)
    if isinstance(container, list) and last.isdigit() and int(last) < len(container):
        container[int(last)] = value
    elif isinstance(container, dict) and last in container:
        container[last] = value
    else:
        raise KeyError(f"no value at path {path!r}")


def item_values(schema: type[ExtractionModel], document: Mapping[str, object]) -> ItemValues:
    """Split an extraction into a saved item's columns.

    Args:
        schema: The schema the extraction was checked against.
        document: The extraction as JSON, normally after normalization.

    Returns:
        The summary, category, tags and `fields` of the item.

    Raises:
        ValueError: If a nested open value would be listed under a key that
            the schema already uses for a field of its own.
    """
    fields: JsonDocument = {
        name: value for name, value in document.items() if name not in SHARED_FIELDS
    }
    for open_value in find_open_values(schema, document):
        if not open_value.nested:
            continue
        if open_value.field in schema.model_fields:
            raise ValueError(
                f"nested open values of field {open_value.field!r} can't be listed in `fields`, "
                "because the schema has a field of that name"
            )
        listed = fields.setdefault(open_value.field, [])
        if isinstance(listed, list) and open_value.wording not in listed:
            listed.append(open_value.wording)
    tags = document.get(_TAGS)
    return ItemValues(
        summary=str(document[_SUMMARY]),
        category=str(document[_CATEGORY]),
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        fields=fields,
    )


def document_of_item(
    schema: type[ExtractionModel],
    *,
    summary: str | None,
    category: str | None,
    tags: list[str],
    fields: Mapping[str, object],
) -> JsonDocument:
    """Rebuild an item's extraction from its columns, as `item_values` split it.

    Args:
        schema: The schema of the item's type.
        summary: `saved_item.summary`.
        category: `saved_item.category`.
        tags: `saved_item.tags`.
        fields: `saved_item.fields`.

    Returns:
        The extraction as JSON, without the lists of nested open values.
    """
    document: JsonDocument = {
        name: value for name, value in fields.items() if name in schema.model_fields
    }
    document[_SUMMARY] = summary
    document[_CATEGORY] = category
    document[_TAGS] = list(tags)
    return document


def vocabulary_pairs(
    *, category: str | None, subtype: str | None, tags: list[str], fields: Mapping[str, object]
) -> set[tuple[str, str]]:
    """List the `(vocabulary field, value)` pairs an item uses, by record 0007's rule.

    It is the rule `delete_user_data` uses to lower the counts, so a count
    raised here is lowered by the same amount when the item goes.

    Args:
        category: `saved_item.category`.
        subtype: `saved_item.subtype`.
        tags: `saved_item.tags`.
        fields: `saved_item.fields`.

    Returns:
        Each pair once, also when the item has a value twice.
    """
    pairs: set[tuple[str, str]] = set()
    if category is not None:
        pairs.add((CATEGORY_VOCABULARY_FIELD, category))
    if subtype is not None:
        pairs.add((SUBTYPE_VOCABULARY_FIELD, subtype))
    pairs.update((TAG_VOCABULARY_FIELD, tag) for tag in tags)
    for key, value in fields.items():
        if isinstance(value, str):
            pairs.add((key, value))
        elif isinstance(value, list):
            pairs.update((key, entry) for entry in value if isinstance(entry, str))
    return pairs


def _walk(
    schema: type[ExtractionModel],
    document: Mapping[str, object],
    *,
    prefix: tuple[str, ...],
    nested: bool,
) -> Iterator[OpenValue]:
    marked = vocabulary_fields(schema)
    for name, model_field in schema.model_fields.items():
        value = document.get(name)
        path = (*prefix, name)
        if name in marked:
            yield from _marked_values(path, marked[name], value, nested=nested)
            continue
        child_schema = _nested_schema(model_field.annotation)
        if child_schema is None:
            continue
        if isinstance(value, list):
            for index, entry in enumerate(value):
                if isinstance(entry, Mapping):
                    yield from _walk(child_schema, entry, prefix=(*path, str(index)), nested=True)
        elif isinstance(value, Mapping):
            yield from _walk(child_schema, value, prefix=path, nested=True)


def _marked_values(
    path: tuple[str, ...], field: str, value: object, *, nested: bool
) -> Iterator[OpenValue]:
    if isinstance(value, str):
        yield OpenValue(PATH_SEPARATOR.join(path), field, value, nested)
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            if isinstance(entry, str):
                yield OpenValue(PATH_SEPARATOR.join((*path, str(index))), field, entry, nested)


def _nested_schema(annotation: object) -> type[ExtractionModel] | None:
    """Return the schema model inside an annotation such as `tuple[PlaceMention, ...]`."""
    if isinstance(annotation, type) and issubclass(annotation, ExtractionModel):
        return annotation
    if get_origin(annotation) in {tuple, list, Union, UnionType}:
        for argument in get_args(annotation):
            if argument is Ellipsis or argument is NoneType:
                continue
            found = _nested_schema(argument)
            if found is not None:
                return found
    return None


def _child(container: object, part: str) -> object:
    if isinstance(container, list) and part.isdigit() and int(part) < len(container):
        return container[int(part)]
    if isinstance(container, dict) and part in container:
        return container[part]
    raise KeyError(f"no value at path part {part!r}")
