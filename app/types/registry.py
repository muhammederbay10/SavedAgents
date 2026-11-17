"""The content-type registry: the one place that knows which built-in types exist.

The pipeline, the search and the response agent ask the registry for a type and
read its declaration. They never name a type themselves.

A post does not need a built-in type. When the decision model answers "none of
these", the post has no type, and it is still extracted with its category, tags
and summary.
"""

from collections.abc import Iterable

from app.types.base import ContentType, ContentTypeDefinitionError, Extraction
from app.types.movie import MOVIE
from app.types.place import PLACE
from app.types.study import STUDY


class UnknownContentTypeError(LookupError):
    """No content type is registered under the requested name."""


class ContentTypeRegistry:
    """A fixed set of built-in content types, looked up by name."""

    def __init__(self, content_types: Iterable[ContentType]) -> None:
        """Register the given types.

        Args:
            content_types: The types to register, in the order they are listed.

        Raises:
            ContentTypeDefinitionError: If two types share a name.
        """
        self._by_name: dict[str, ContentType] = {}
        for content_type in content_types:
            if content_type.name in self._by_name:
                raise ContentTypeDefinitionError(
                    f"Content type '{content_type.name}' is registered twice"
                )
            self._by_name[content_type.name] = content_type

    def all(self) -> tuple[ContentType, ...]:
        """List every registered type, in registration order.

        Returns:
            The registered types.
        """
        return tuple(self._by_name.values())

    def get(self, name: str) -> ContentType:
        """Look a type up by its name.

        Args:
            name: The type's name, e.g. `place`.

        Returns:
            The type registered under that name.

        Raises:
            UnknownContentTypeError: If no type has that name.
        """
        try:
            return self._by_name[name]
        except KeyError as error:
            known = ", ".join(self._by_name)
            raise UnknownContentTypeError(
                f"Unknown content type '{name}'. Registered types: {known}"
            ) from error

    def extraction_schema(self, type_name: str | None) -> type[Extraction]:
        """Pick the schema the text model fills in for a post.

        Args:
            type_name: The built-in type the decision model chose, or `None`
                when it answered "none of these".

        Returns:
            The type's schema, or the shared `Extraction` (summary, category
            and tags) for a post without a built-in type.

        Raises:
            UnknownContentTypeError: If `type_name` is given but not registered.
        """
        if type_name is None:
            return Extraction
        return self.get(type_name).extraction_schema


CONTENT_TYPES = ContentTypeRegistry((PLACE, MOVIE, STUDY))
