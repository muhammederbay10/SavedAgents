"""Building blocks that every content type is declared with.

A content type is data, not code paths: the pipeline and the search read these
declarations, so adding a type never needs an `if type == ...` anywhere else.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MINIMUM_SUBTYPE_OPTIONS = 2

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ContentTypeDefinitionError(ValueError):
    """A content type, or one of its parts, is declared in a way that cannot work."""


def values_of(vocabulary: type[StrEnum]) -> tuple[str, ...]:
    """List the values of a closed vocabulary, in declaration order.

    Args:
        vocabulary: The enum that holds the allowed values.

    Returns:
        The enum's values as plain strings.
    """
    return tuple(member.value for member in vocabulary)


def _require_unique(values: tuple[str, ...], what: str) -> None:
    """Reject a declaration that names the same value twice.

    Args:
        values: The declared values.
        what: What the values are, for the error message.

    Raises:
        ContentTypeDefinitionError: If a value appears more than once.
    """
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ContentTypeDefinitionError(f"Duplicate {what}: {', '.join(duplicates)}")


@dataclass(frozen=True)
class SubtypeQuestion:
    """The choice question the decision model answers to pick a post's subtype.

    Attributes:
        text: The question, as it is put to the decision model.
        options: The subtypes to choose from. Exactly one is picked.

    Raises:
        ContentTypeDefinitionError: If there are fewer than two options, or an
            option is listed twice.
    """

    text: str
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        """Check that the options form a real choice."""
        if len(self.options) < MINIMUM_SUBTYPE_OPTIONS:
            raise ContentTypeDefinitionError(
                f"A subtype question needs at least {MINIMUM_SUBTYPE_OPTIONS} options"
            )
        _require_unique(self.options, "subtype options")


@dataclass(frozen=True)
class TagQuestion:
    """A yes/no question the decision model answers. "Yes" adds the tag to the post.

    Attributes:
        tag: The tag that is stored when the answer is yes.
        text: The question, as it is put to the decision model.
    """

    tag: str
    text: str


class FilterKind(StrEnum):
    """The kind of value a search filter takes.

    Attributes:
        CHOICE: One of the filter's allowed values.
        BOOLEAN: True or false.
        INTEGER: A whole number.
        TEXT: Free text.
        LOCATION: A point and a distance around it.
    """

    CHOICE = "choice"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    TEXT = "text"
    LOCATION = "location"


@dataclass(frozen=True)
class SearchFilter:
    """A filter that a content type offers to search.

    Attributes:
        name: The key used in a search's `filters` argument, e.g. `district`.
        kind: The kind of value the filter takes.
        description: What the filter selects, written for the response agent.
        allowed_values: The values a `CHOICE` filter accepts. User input is
            normalized to one of them. Empty for every other kind.

    Raises:
        ContentTypeDefinitionError: If a `CHOICE` filter has no allowed values,
            another kind has some, or a value is listed twice.
    """

    name: str
    kind: FilterKind
    description: str
    allowed_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Check that allowed values are given exactly when the kind needs them."""
        is_choice = self.kind is FilterKind.CHOICE
        if is_choice and not self.allowed_values:
            raise ContentTypeDefinitionError(
                f"Filter '{self.name}' is a choice and needs allowed values"
            )
        if not is_choice and self.allowed_values:
            raise ContentTypeDefinitionError(
                f"Filter '{self.name}' is not a choice, so it cannot have allowed values"
            )
        _require_unique(self.allowed_values, f"allowed values in filter '{self.name}'")


class ExtractionModel(BaseModel):
    """Shared settings for everything the text model returns.

    Unknown keys are rejected, so a reply that does not follow the schema fails
    loudly instead of being stored half-understood.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class EntityMention(ExtractionModel):
    """One real-world thing a post mentions, before a resolver has looked it up.

    Types with a resolver subclass this with the fields their resolver searches by.
    """


class Extraction(ExtractionModel):
    """What the text model extracts from one post. Every type's schema extends it.

    Attributes:
        summary: One or two sentences on what the post is about.
    """

    summary: NonEmptyText = Field(
        description="One or two sentences on what the post is about, in the post's language."
    )


class EntityExtraction(Extraction):
    """The extraction of a type whose posts point at real-world entities.

    Attributes:
        entities: Every entity the post mentions, in the order they appear. A
            list post ("5 movies for the weekend") has several. It is empty
            when the text did not name one.
    """

    entities: tuple[EntityMention, ...]


@dataclass(frozen=True)
class ContentType:
    """Everything the system needs to know about one kind of saved post.

    Attributes:
        name: The identifier stored on a saved item, e.g. `place`.
        description: What the type covers. The decision model sees it as one of
            the options when it picks a post's type.
        extraction_schema: The Pydantic model the text model fills in.
        subtype_question: The question that picks the subtype, or `None` when
            the type has no subtypes.
        tag_questions: The yes/no questions that tag a post. May be empty.
        resolver: The name of the resolver in `app/resolvers/` that looks up
            this type's entities, e.g. `tmdb`. `None` means the type has no
            real-world entity, and everything it knows stays on the saved item.
        search_filters: The filters the type offers to search.

    Raises:
        ContentTypeDefinitionError: If two tags or two filters share a name, or
            the resolver and the extraction schema disagree on whether the type
            has entities.
    """

    name: str
    description: str
    extraction_schema: type[Extraction]
    subtype_question: SubtypeQuestion | None
    tag_questions: tuple[TagQuestion, ...]
    resolver: str | None
    search_filters: tuple[SearchFilter, ...]

    def __post_init__(self) -> None:
        """Check that the parts of the declaration fit together."""
        _require_unique(
            tuple(question.tag for question in self.tag_questions),
            f"tags in content type '{self.name}'",
        )
        _require_unique(
            tuple(search_filter.name for search_filter in self.search_filters),
            f"filters in content type '{self.name}'",
        )
        self._require_resolver_matches_schema()

    def _require_resolver_matches_schema(self) -> None:
        """Check that a resolver is declared exactly when the schema lists entities.

        Raises:
            ContentTypeDefinitionError: If only one of the two is present.
        """
        extracts_entities = issubclass(self.extraction_schema, EntityExtraction)
        if self.resolver is not None and not extracts_entities:
            raise ContentTypeDefinitionError(
                f"Content type '{self.name}' has a resolver, so its extraction schema "
                "must extend EntityExtraction"
            )
        if self.resolver is None and extracts_entities:
            raise ContentTypeDefinitionError(
                f"Content type '{self.name}' extracts entities, so it needs a resolver"
            )
