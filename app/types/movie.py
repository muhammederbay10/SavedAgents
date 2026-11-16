"""The `movie` content type: a movie or a series to watch."""

from enum import StrEnum

from pydantic import Field

from app.types.base import (
    ContentType,
    EntityExtraction,
    EntityMention,
    FilterKind,
    NonEmptyText,
    SearchFilter,
    SubtypeQuestion,
    TagQuestion,
    values_of,
)

TMDB_RESOLVER = "tmdb"

# The first films were shot in 1888. A year hint outside this range is a
# misreading (a runtime, a count, a typo), not a release year.
EARLIEST_RELEASE_YEAR = 1888
LATEST_RELEASE_YEAR = 2100


class MovieSubtype(StrEnum):
    """Whether a title is a movie or a series."""

    MOVIE = "movie"
    SERIES = "series"


class Genre(StrEnum):
    """The genres a title can be filtered by: TMDB's movie and TV genres as one list."""

    ACTION = "action"
    ADVENTURE = "adventure"
    ANIMATION = "animation"
    COMEDY = "comedy"
    CRIME = "crime"
    DOCUMENTARY = "documentary"
    DRAMA = "drama"
    FAMILY = "family"
    FANTASY = "fantasy"
    HISTORY = "history"
    HORROR = "horror"
    KIDS = "kids"
    MUSIC = "music"
    MYSTERY = "mystery"
    REALITY = "reality"
    ROMANCE = "romance"
    SCIENCE_FICTION = "science fiction"
    THRILLER = "thriller"
    WAR = "war"
    WESTERN = "western"


class StreamingPlatform(StrEnum):
    """The streaming platforms in Turkey a title can be filtered by."""

    NETFLIX = "Netflix"
    PRIME_VIDEO = "Prime Video"
    DISNEY_PLUS = "Disney+"
    HBO_MAX = "HBO Max"
    APPLE_TV_PLUS = "Apple TV+"
    MUBI = "MUBI"
    EXXEN = "Exxen"
    GAIN = "Gain"
    TABII = "Tabii"
    TOD = "TOD"
    TV_PLUS = "TV+"
    PUHUTV = "puhutv"


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
    subtype_question=SubtypeQuestion(
        text="Is the post about a movie or a series?",
        options=values_of(MovieSubtype),
    ),
    tag_questions=(
        TagQuestion(
            tag="review",
            text="Does the creator give their own opinion or rating of the title?",
        ),
        TagQuestion(
            tag="clip",
            text="Does the post show a scene, a trailer or another clip from the title?",
        ),
    ),
    resolver=TMDB_RESOLVER,
    search_filters=(
        SearchFilter(
            name="genre",
            kind=FilterKind.CHOICE,
            description="A genre of the movie or series.",
            allowed_values=values_of(Genre),
        ),
        SearchFilter(
            name="year",
            kind=FilterKind.INTEGER,
            description="The year the movie or series was first released.",
        ),
        SearchFilter(
            name="streaming",
            kind=FilterKind.CHOICE,
            description="A streaming platform that has the title in Turkey.",
            allowed_values=values_of(StreamingPlatform),
        ),
        SearchFilter(
            name="watched",
            kind=FilterKind.BOOLEAN,
            description="True for titles the user has watched, false for the ones still to watch.",
        ),
    ),
)
