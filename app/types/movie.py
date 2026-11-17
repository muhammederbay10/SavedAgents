"""The `movie` content type: a movie or a series to watch."""

from pydantic import Field

from app.types.base import (
    ContentType,
    EntityExtraction,
    EntityMention,
    FilterKind,
    NonEmptyText,
    SearchFilter,
)

TMDB_RESOLVER = "tmdb"
GENRE_VOCABULARY = "genre"

# The first films were shot in 1888. A year hint outside this range is a
# misreading (a runtime, a count, a typo), not a release year.
EARLIEST_RELEASE_YEAR = 1888
LATEST_RELEASE_YEAR = 2100


class MovieMention(EntityMention):
    """One movie or series a post mentions."""

    title: NonEmptyText = Field(
        description="The title of the movie or series exactly as the post writes or says it."
    )
    year_hint: int | None = Field(
        ge=EARLIEST_RELEASE_YEAR,
        le=LATEST_RELEASE_YEAR,
        description="The release year, only if the post states it. Null otherwise.",
    )


class MovieExtraction(EntityExtraction):
    """What the text model extracts from a post about movies or series."""

    entities: tuple[MovieMention, ...] = Field(
        description=(
            "Every movie or series the post mentions, in order. A list post has several. "
            "Empty if no title is named."
        )
    )


MOVIE = ContentType(
    name="movie",
    description="A movie or a series to watch.",
    extraction_schema=MovieExtraction,
    resolver=TMDB_RESOLVER,
    search_filters=(
        SearchFilter(
            name="genre",
            kind=FilterKind.VALUE,
            description="A genre of the movie or series.",
            vocabulary=GENRE_VOCABULARY,
        ),
        SearchFilter(
            name="year",
            kind=FilterKind.INTEGER,
            description="The year the movie or series was first released.",
        ),
        SearchFilter(
            name="streaming",
            kind=FilterKind.VALUE,
            description="A streaming platform that has the title in the user's country.",
        ),
        SearchFilter(
            name="watched",
            kind=FilterKind.BOOLEAN,
            description="True for titles the user has watched, false for the ones still to watch.",
        ),
    ),
)
