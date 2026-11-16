"""The `other` content type: every post that fits none of the specific types."""

from pydantic import Field

from app.types.base import (
    ContentType,
    Extraction,
    FilterKind,
    NonEmptyText,
    SearchFilter,
)


class OtherExtraction(Extraction):
    """What the text model extracts from a post of no specific type."""

    tags: tuple[NonEmptyText, ...] = Field(
        description=(
            "A few short labels for what the post is about, e.g. 'recipe', 'travel' "
            "or 'home workout'. May be empty."
        )
    )


OTHER = ContentType(
    name="other",
    description="Anything that fits none of the other types.",
    extraction_schema=OtherExtraction,
    # The tags of an `other` post are free text written by the text model. The
    # decision model can only pick from fixed options, so it has nothing to ask.
    subtype_question=None,
    tag_questions=(),
    resolver=None,
    search_filters=(
        SearchFilter(
            name="tags",
            kind=FilterKind.TEXT,
            description="A label the post was tagged with, e.g. 'recipe'.",
        ),
    ),
)
