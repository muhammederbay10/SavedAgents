"""The `study` content type: a tip, tutorial, explanation or course for learning something."""

from typing import Annotated

from pydantic import Field

from app.types.base import (
    ContentType,
    Extraction,
    FilterKind,
    NonEmptyText,
    SearchFilter,
    Vocabulary,
)

SUBJECT_VOCABULARY = "subject"
FORMAT_VOCABULARY = "format"
EXAM_VOCABULARY = "exam"


class StudyExtraction(Extraction):
    """What the text model extracts from a study post."""

    subject: Annotated[NonEmptyText, Vocabulary(SUBJECT_VOCABULARY)] = Field(
        description=(
            "The subject the post teaches, in a few plain words of your own, e.g. 'maths', "
            "'organic chemistry' or 'study skills'. There is no list to pick from."
        )
    )
    topic: NonEmptyText = Field(
        description="The specific topic, e.g. 'integrals', 'phrasal verbs' or 'spaced repetition'."
    )
    format: Annotated[NonEmptyText, Vocabulary(FORMAT_VOCABULARY)] = Field(
        description=(
            "The form the post takes, in a few plain words of your own, e.g. 'tip', "
            "'tutorial', 'explainer' or 'course'. There is no list to pick from."
        )
    )
    key_points: tuple[NonEmptyText, ...] = Field(
        description="The main things the post teaches, one short sentence each. May be empty."
    )
    resources: tuple[NonEmptyText, ...] = Field(
        description=(
            "Books, apps, channels, sites or courses the post mentions by name. May be empty."
        )
    )
    exam: Annotated[NonEmptyText | None, Vocabulary(EXAM_VOCABULARY)] = Field(
        description=(
            "The exam or course the post prepares for, as the post names it, in any country, "
            "e.g. 'IELTS', 'SAT' or 'YKS'. Null if it names none."
        )
    )


STUDY = ContentType(
    name="study",
    description="Study material: a tip, tutorial, explanation or course for learning something.",
    extraction_schema=StudyExtraction,
    resolver=None,
    search_filters=(
        SearchFilter(
            name="subject",
            kind=FilterKind.VALUE,
            description="The subject the post teaches.",
            vocabulary=SUBJECT_VOCABULARY,
        ),
        SearchFilter(
            name="topic",
            kind=FilterKind.TEXT,
            description="The specific topic inside the subject, e.g. 'integrals'.",
        ),
        SearchFilter(
            name="format",
            kind=FilterKind.VALUE,
            description="The form the post takes, e.g. a tip or a tutorial.",
            vocabulary=FORMAT_VOCABULARY,
        ),
        SearchFilter(
            name="exam",
            kind=FilterKind.VALUE,
            description="The exam or course the post prepares for.",
            vocabulary=EXAM_VOCABULARY,
        ),
    ),
)
