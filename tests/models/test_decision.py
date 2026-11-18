import pytest

from app.models.base import (
    Audio,
    ChoiceAnswer,
    ChoiceQuestion,
    Image,
    ScoreAnswer,
    ScoreQuestion,
    Unanswered,
    Video,
    YesNoAnswer,
    YesNoQuestion,
)
from app.models.decision import OpenRouterClassifier
from app.models.errors import ModelRequestError
from app.models.openrouter import OpenRouterTransport
from tests.models.conftest import Connect
from tests.models.support import (
    CHAT_COMPLETIONS_URL,
    DECISIONS_URL,
    TIMEOUT_SECONDS,
    error_response,
    fixture_response,
    json_response,
    load_fixture,
)

DECISION_MODEL = "google/gemini-decision-1"

IS_LIST = YesNoQuestion(key="is_list", instructions="Is this a list of several things?")
CONTENT_TYPE = ChoiceQuestion(
    key="content_type",
    instructions="Which built-in type fits?",
    options={"place": "A venue", "movie": "A film or series", "none": "None of these"},
)
CAPTION_QUALITY = ScoreQuestion(
    key="caption_quality",
    instructions="How much does the caption say?",
    levels=["nothing", "a hint", "the full name"],
)
IS_PRIVATE = YesNoQuestion(key="is_private", instructions="Does it name a private person?")


def classifier(transport: OpenRouterTransport) -> OpenRouterClassifier:
    return OpenRouterClassifier(
        transport, url=DECISIONS_URL, model=DECISION_MODEL, timeout_seconds=TIMEOUT_SECONDS
    )


async def test_request_goes_to_the_decisions_endpoint_not_chat_completions(
    connect: Connect,
) -> None:
    server, transport = connect([fixture_response("decisions_response.json")])

    await classifier(transport).decide(["a caption"], [IS_LIST])

    assert server.only_request.url == DECISIONS_URL
    assert server.only_request.url != CHAT_COMPLETIONS_URL
    assert "messages" not in server.only_request.body


async def test_request_holds_state_parts_and_typed_questions_by_key(connect: Connect) -> None:
    server, transport = connect([fixture_response("decisions_response.json")])
    state = [
        "a caption",
        Video(data=b"video-bytes", mime_type="video/mp4"),
        Image(data=b"image-bytes", mime_type="image/jpeg"),
        Audio(data=b"audio-bytes", format="wav"),
    ]

    await classifier(transport).decide(state, [IS_LIST, CONTENT_TYPE, CAPTION_QUALITY])

    assert server.only_request.body == {
        "model": DECISION_MODEL,
        "state": [
            "a caption",
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,dmlkZW8tYnl0ZXM="}},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW1hZ2UtYnl0ZXM="}},
            {"type": "input_audio", "input_audio": {"data": "YXVkaW8tYnl0ZXM=", "format": "wav"}},
        ],
        "questions": {
            "is_list": {"type": "noul", "instructions": "Is this a list of several things?"},
            "content_type": {
                "type": "choice",
                "instructions": "Which built-in type fits?",
                "criteria": {
                    "place": "A venue",
                    "movie": "A film or series",
                    "none": "None of these",
                },
            },
            "caption_quality": {
                "type": "score",
                "instructions": "How much does the caption say?",
                "criteria": ["nothing", "a hint", "the full name"],
            },
        },
    }


async def test_answers_come_back_typed_with_their_probabilities(connect: Connect) -> None:
    _, transport = connect([fixture_response("decisions_response.json")])

    result = await classifier(transport).decide(
        ["a caption"], [IS_LIST, CONTENT_TYPE, CAPTION_QUALITY, IS_PRIVATE]
    )

    assert result.answers == {
        "is_list": YesNoAnswer(probability_yes=0.07, confidence=0.93),
        "content_type": ChoiceAnswer(
            choice="place",
            probabilities={"place": 0.88, "movie": 0.04, "none": 0.08},
            confidence=0.88,
        ),
        "caption_quality": ScoreAnswer(
            score=1.6, probabilities={0: 0.1, 1: 0.2, 2: 0.7}, confidence=0.7
        ),
        "is_private": Unanswered(reason="refused"),
    }
    assert result.model == "google/gemini-decision-1-20261111"
    assert result.cost_usd == 0.000214


async def test_question_without_an_answer_is_unanswered_and_the_rest_stay(
    connect: Connect,
) -> None:
    _, transport = connect([fixture_response("decisions_response.json")])
    not_in_response = YesNoQuestion(key="has_text_overlay", instructions="Is there overlay text?")

    result = await classifier(transport).decide(["a caption"], [IS_LIST, not_in_response])

    assert result.answers["has_text_overlay"] == Unanswered(reason="missing")
    assert result.answers["is_list"] == YesNoAnswer(probability_yes=0.07, confidence=0.93)


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (IS_LIST, {"type": "noul", "noul": 1.4}),
        (IS_LIST, {"type": "noul", "noul": "yes"}),
        (IS_LIST, {"type": "choice", "choice": "place", "probabilities": {"place": 1.0}}),
        (CONTENT_TYPE, {"type": "choice", "choice": "recipe", "probabilities": {"place": 1.0}}),
        (CONTENT_TYPE, {"type": "choice", "choice": "place", "probabilities": {"recipe": 1.0}}),
        (CONTENT_TYPE, {"type": "choice", "choice": "place", "probabilities": {"place": -0.1}}),
        (CONTENT_TYPE, {"type": "choice", "choice": "place"}),
        (CAPTION_QUALITY, {"type": "score", "score": 3.0, "probabilities": {"0": 1.0}}),
        (CAPTION_QUALITY, {"type": "score", "score": 1.0, "probabilities": {"5": 1.0}}),
        (CAPTION_QUALITY, {"type": "score", "score": 1.0, "probabilities": {"low": 1.0}}),
        (CAPTION_QUALITY, {"type": "something_new"}),
    ],
)
async def test_unusable_answer_is_marked_invalid_instead_of_trusted(
    connect: Connect,
    question: YesNoQuestion | ChoiceQuestion | ScoreQuestion,
    answer: dict[str, object],
) -> None:
    body = load_fixture("decisions_response.json")
    body["answers"] = {question.key: answer}
    _, transport = connect([json_response(body)])

    result = await classifier(transport).decide(["a caption"], [question])

    assert result.answers[question.key] == Unanswered(reason="invalid")


async def test_missing_confidence_is_reported_as_none(connect: Connect) -> None:
    body = load_fixture("decisions_response.json")
    body["answers"] = {"is_list": {"type": "noul", "noul": 0.5}}
    _, transport = connect([json_response(body)])

    result = await classifier(transport).decide(["a caption"], [IS_LIST])

    assert result.answers["is_list"] == YesNoAnswer(probability_yes=0.5, confidence=None)


async def test_no_questions_or_a_repeated_key_is_refused_before_any_request(
    connect: Connect,
) -> None:
    server, transport = connect([])

    with pytest.raises(ValueError, match="at least one question"):
        await classifier(transport).decide(["a caption"], [])
    with pytest.raises(ValueError, match="different key"):
        await classifier(transport).decide(["a caption"], [IS_LIST, IS_LIST])

    assert server.requests == []


async def test_rejected_request_raises_a_model_error_naming_the_operation(
    connect: Connect,
) -> None:
    _, transport = connect([error_response(400)])

    with pytest.raises(ModelRequestError, match="decide with google/gemini-decision-1") as raised:
        await classifier(transport).decide(["a caption"], [IS_LIST])

    assert raised.value.operation == "decide"
