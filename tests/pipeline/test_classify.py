"""Classify: one decision call for the type (or none) and is-list, and no speech question."""

import pytest

from app.models.base import ChoiceQuestion, Video, YesNoAnswer
from app.pipeline.classify import (
    IS_LIST_QUESTION_KEY,
    NONE_OF_THESE,
    TYPE_QUESTION_KEY,
    Classification,
    PostClassifier,
)
from app.types.registry import CONTENT_TYPES, ContentTypeRegistry
from tests.pipeline.support import TEST_PROMPTS, ScriptedClassifier

IS_LIST_MIN_PROBABILITY = 0.5
VIDEO = Video(data=b"synthetic video", mime_type="video/mp4")


def post_classifier(classifier: ScriptedClassifier) -> PostClassifier:
    return PostClassifier(
        classifier, CONTENT_TYPES, TEST_PROMPTS, is_list_min_probability=IS_LIST_MIN_PROBABILITY
    )


async def test_one_call_asks_the_type_and_is_list_with_the_media_and_text() -> None:
    classifier = ScriptedClassifier(content_type="movie", is_list_probability=0.8)

    result = await post_classifier(classifier).classify([VIDEO], "5 films", "Movies")

    assert len(classifier.calls) == 1
    state, questions = classifier.calls[0]
    assert state[0] is VIDEO
    assert state[1] == "caption=5 films\ncollection=Movies"
    assert [question.key for question in questions] == [TYPE_QUESTION_KEY, IS_LIST_QUESTION_KEY]
    assert result.type_name == "movie"
    assert result.is_list


async def test_the_type_options_are_the_registry_plus_none_of_these() -> None:
    classifier = ScriptedClassifier()

    await post_classifier(classifier).classify([], None, None)

    type_question = classifier.calls[0][1][0]
    assert isinstance(type_question, ChoiceQuestion)
    expected = {content_type.name for content_type in CONTENT_TYPES.all()} | {NONE_OF_THESE}
    assert set(type_question.options) == expected
    assert type_question.options[NONE_OF_THESE] == TEST_PROMPTS.classify_none_option


async def test_none_of_these_is_a_post_without_a_built_in_type() -> None:
    result = await post_classifier(ScriptedClassifier(content_type=NONE_OF_THESE)).classify(
        [], "a car review", None
    )

    assert result.type_name is None
    assert result.type_unanswered is None


async def test_the_decision_model_is_never_asked_whether_a_post_has_speech() -> None:
    classifier = ScriptedClassifier(
        extra_answers={"has_speech": YesNoAnswer(probability_yes=0.86, confidence=None)}
    )

    await post_classifier(classifier).classify([VIDEO], None, None)

    assert classifier.asked_keys() == [TYPE_QUESTION_KEY, IS_LIST_QUESTION_KEY]


async def test_an_unanswered_type_question_leaves_the_post_without_a_type() -> None:
    classifier = ScriptedClassifier(content_type="place", unanswered={TYPE_QUESTION_KEY})

    result = await post_classifier(classifier).classify([], "caption", None)

    assert result.type_name is None
    assert result.type_unanswered == "refused"


async def test_the_classification_survives_its_step_output() -> None:
    result = await post_classifier(ScriptedClassifier(content_type="study")).classify([], "x", None)

    restored = Classification.from_output(result.to_output(TEST_PROMPTS.version))

    assert restored == result
    assert result.to_output(TEST_PROMPTS.version)["prompts_version"] == TEST_PROMPTS.version


def test_a_type_named_like_the_none_option_is_refused() -> None:
    clashing = ContentTypeRegistry(
        [
            type(CONTENT_TYPES.get("study"))(
                name=NONE_OF_THESE,
                description="clash",
                extraction_schema=CONTENT_TYPES.get("study").extraction_schema,
                resolver=None,
                search_filters=(),
            )
        ]
    )

    with pytest.raises(ValueError, match="none"):
        PostClassifier(ScriptedClassifier(), clashing, TEST_PROMPTS, is_list_min_probability=0.5)
