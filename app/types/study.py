"""The `study` content type: a tip, tutorial, explanation or course for learning something."""

from enum import StrEnum

from pydantic import Field

from app.types.base import (
    ContentType,
    Extraction,
    FilterKind,
    NonEmptyText,
    SearchFilter,
    SubtypeQuestion,
    TagQuestion,
    values_of,
)


class StudyFormat(StrEnum):
    """The formats a study post comes in. A post's format is its subtype."""

    TIP = "tip"
    TUTORIAL = "tutorial"
    EXPLAINER = "explainer"
    COURSE = "course"


class StudySubject(StrEnum):
    """The subjects a study post can be filtered by. `OTHER` covers everything else."""

    MATHS = "maths"
    PHYSICS = "physics"
    CHEMISTRY = "chemistry"
    BIOLOGY = "biology"
    TURKISH = "turkish"
    ENGLISH = "english"
    FOREIGN_LANGUAGES = "foreign languages"
    HISTORY = "history"
    GEOGRAPHY = "geography"
    PHILOSOPHY = "philosophy"
    PROGRAMMING = "programming"
    STUDY_SKILLS = "study skills"
    OTHER = "other"


class Exam(StrEnum):
    """The exams a study post can prepare for."""

    YKS = "YKS"
    LGS = "LGS"
    KPSS = "KPSS"
    ALES = "ALES"
    DGS = "DGS"
    YDS = "YDS"
    YOKDIL = "YÖKDİL"
    IELTS = "IELTS"
    TOEFL = "TOEFL"


class StudyExtraction(Extraction):
    """What the text model extracts from a study post."""

    subject: StudySubject = Field(
        description=(
            "The subject the post teaches. Use 'study skills' for advice on how to study "
            "and 'other' when no subject fits."
        )
    )
    topic: NonEmptyText = Field(
        description="The specific topic, e.g. 'integrals', 'phrasal verbs' or 'spaced repetition'."
    )
    key_points: tuple[NonEmptyText, ...] = Field(
        description="The main things the post teaches, one short sentence each. May be empty."
    )
    resources: tuple[NonEmptyText, ...] = Field(
        description=(
            "Books, apps, channels, sites or courses the post mentions by name. May be empty."
        )
    )
    exam: Exam | None = Field(
        description="The exam the post prepares for, only if it names one. Null otherwise."
    )


STUDY = ContentType(
    name="study",
    description="Study material: a tip, tutorial, explanation or course for learning something.",
    extraction_schema=StudyExtraction,
    subtype_question=SubtypeQuestion(
        text=(
            "What format is the post: a short tip, a step-by-step tutorial, an explainer "
            "of one idea, or a course with several lessons?"
        ),
        options=values_of(StudyFormat),
    ),
    tag_questions=(
        TagQuestion(
            tag="practice",
            text="Does the post solve or share practice questions?",
        ),
        TagQuestion(
            tag="notes",
            text="Does the post show written notes, a summary sheet or a mind map?",
        ),
        TagQuestion(
            tag="resources",
            text="Does the post recommend books, apps, channels or other study resources?",
        ),
    ),
    resolver=None,
    search_filters=(
        SearchFilter(
            name="subject",
            kind=FilterKind.CHOICE,
            description="The subject the post teaches.",
            allowed_values=values_of(StudySubject),
        ),
        SearchFilter(
            name="topic",
            kind=FilterKind.TEXT,
            description="The specific topic inside the subject, e.g. 'integrals'.",
        ),
        SearchFilter(
            name="format",
            kind=FilterKind.CHOICE,
            description="The post's format.",
            allowed_values=values_of(StudyFormat),
        ),
        SearchFilter(
            name="exam",
            kind=FilterKind.CHOICE,
            description="The exam the post prepares for.",
            allowed_values=values_of(Exam),
        ),
    ),
)
