"""The content-type registry: the one place that knows which types exist.

The pipeline, the search and the response agent ask the registry for a type and
read its declaration. They never name a type themselves.
"""

from collections.abc import Iterable

from app.types.base import ContentType, ContentTypeDefinitionError
from app.types.movie import MOVIE
from app.types.other import OTHER
from app.types.place import PLACE
from app.types.study import STUDY


class UnknownContentTypeError(LookupError):
    """No content type is registered under the requested name."""


class ContentTypeRegistry:
    """A fixed set of content types, looked up by name."""

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


CONTENT_TYPES = ContentTypeRegistry((PLACE, MOVIE, STUDY, OTHER))
