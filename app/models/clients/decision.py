"""The decision model client: Gemini Decision 1 through OpenRouter's decisions endpoint.

This endpoint is not chat completions. It has its own URL and its own contract
(OpenRouter docs, "Decisions API", 17 Nov 2026):

- Request: `model`, `state` (an array of plain strings and media parts) and
  `questions` (an object keyed by question id). A question's `type` is `noul`
  (yes/no), `choice` or `score`.
- Response: `answers` keyed by question id. A `noul` answer holds the
  probability of yes in `noul`; `choice` holds the chosen key and a
  `probabilities` object; `score` holds a position on the scale and
  `probabilities` by level index; `refusal` means the model declined that one
  question. `model` names the version that answered.

A refused, missing or malformed answer comes back as `Unanswered` for that
question only. The other answers of the same call stay usable.
"""

from collections.abc import Sequence

from app.models.base import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    Classifier,
    ContentPart,
    DecisionResult,
    Question,
    ScoreAnswer,
    ScoreQuestion,
    Unanswered,
    YesNoAnswer,
    YesNoQuestion,
)
from app.models.openrouter.transport import ModelCall, OpenRouterTransport
from app.models.openrouter.wire import (
    JsonObject,
    cost_usd,
    media_part,
    number_or_none,
    object_or_none,
    string_or_none,
)

_OPERATION = "decide"
_YES_NO_TYPE = "noul"
_CHOICE_TYPE = "choice"
_SCORE_TYPE = "score"
_REFUSAL_TYPE = "refusal"


class OpenRouterClassifier(Classifier):
    """Asks typed questions through OpenRouter's decisions endpoint."""

    def __init__(
        self,
        transport: OpenRouterTransport,
        *,
        url: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        """Create the client.

        Args:
            transport: The shared OpenRouter transport.
            url: The full URL of the decisions endpoint.
            model: The decision model's id.
            timeout_seconds: How long one attempt may take.
        """
        self._transport = transport
        self._call = ModelCall(
            operation=_OPERATION, model=model, url=url, timeout_seconds=timeout_seconds
        )

    async def decide(
        self, state: Sequence[ContentPart], questions: Sequence[Question]
    ) -> DecisionResult:
        """Answer every question about the same content in one call.

        Args:
            state: What the questions are about: text and media, in order.
            questions: The questions. Their keys must be unique.

        Returns:
            One answer per question. A question the model did not answer comes
            back as `Unanswered`.

        Raises:
            ValueError: If there are no questions or two share a key.
            ModelError: If the call failed.
        """
        _require_unique_keys(questions)
        payload: JsonObject = {
            "model": self._call.model,
            "state": [part if isinstance(part, str) else media_part(part) for part in state],
            "questions": {question.key: _question_spec(question) for question in questions},
        }
        body = await self._transport.post(self._call, payload)
        answers = object_or_none(body.get("answers")) or {}
        return DecisionResult(
            answers={
                question.key: _parse_answer(question, answers.get(question.key))
                for question in questions
            },
            model=string_or_none(body.get("model")) or self._call.model,
            cost_usd=cost_usd(body),
        )


def _require_unique_keys(questions: Sequence[Question]) -> None:
    if not questions:
        raise ValueError("decide needs at least one question")
    keys = [question.key for question in questions]
    if len(set(keys)) != len(keys):
        raise ValueError("decide needs a different key for every question")


def _question_spec(question: Question) -> JsonObject:
    if isinstance(question, YesNoQuestion):
        return {"type": _YES_NO_TYPE, "instructions": question.instructions}
    if isinstance(question, ChoiceQuestion):
        return {
            "type": _CHOICE_TYPE,
            "instructions": question.instructions,
            "criteria": dict(question.options),
        }
    return {
        "type": _SCORE_TYPE,
        "instructions": question.instructions,
        "criteria": list(question.levels),
    }


def _parse_answer(question: Question, raw_answer: object) -> Answer:
    answer = object_or_none(raw_answer)
    if answer is None:
        return Unanswered(reason="missing")
    if answer.get("type") == _REFUSAL_TYPE:
        return Unanswered(reason="refused")
    if isinstance(question, YesNoQuestion):
        parsed: Answer | None = _parse_yes_no(answer)
    elif isinstance(question, ChoiceQuestion):
        parsed = _parse_choice(question, answer)
    else:
        parsed = _parse_score(question, answer)
    return parsed if parsed is not None else Unanswered(reason="invalid")


def _parse_yes_no(answer: JsonObject) -> YesNoAnswer | None:
    probability_yes = _probability(answer.get(_YES_NO_TYPE))
    if answer.get("type") != _YES_NO_TYPE or probability_yes is None:
        return None
    return YesNoAnswer(probability_yes=probability_yes, confidence=_confidence(answer))


def _parse_choice(question: ChoiceQuestion, answer: JsonObject) -> ChoiceAnswer | None:
    choice = string_or_none(answer.get(_CHOICE_TYPE))
    probabilities = _probabilities(answer)
    if answer.get("type") != _CHOICE_TYPE or choice is None or choice not in question.options:
        return None
    if probabilities is None or not set(probabilities) <= set(question.options):
        return None
    return ChoiceAnswer(choice=choice, probabilities=probabilities, confidence=_confidence(answer))


def _parse_score(question: ScoreQuestion, answer: JsonObject) -> ScoreAnswer | None:
    score = number_or_none(answer.get(_SCORE_TYPE))
    probabilities = _probabilities(answer)
    highest_level = len(question.levels) - 1
    if answer.get("type") != _SCORE_TYPE or score is None or not 0 <= score <= highest_level:
        return None
    if probabilities is None:
        return None
    by_level = _by_level_index(probabilities, highest_level)
    if by_level is None:
        return None
    return ScoreAnswer(score=score, probabilities=by_level, confidence=_confidence(answer))


def _probabilities(answer: JsonObject) -> dict[str, float] | None:
    raw_probabilities = object_or_none(answer.get("probabilities"))
    if raw_probabilities is None:
        return None
    probabilities: dict[str, float] = {}
    for key, raw_value in raw_probabilities.items():
        value = _probability(raw_value)
        if value is None:
            return None
        probabilities[key] = value
    return probabilities


def _by_level_index(probabilities: dict[str, float], highest_level: int) -> dict[int, float] | None:
    by_level: dict[int, float] = {}
    for key, value in probabilities.items():
        if not key.isdecimal() or int(key) > highest_level:
            return None
        by_level[int(key)] = value
    return by_level


def _probability(value: object) -> float | None:
    number = number_or_none(value)
    return number if number is not None and 0.0 <= number <= 1.0 else None


def _confidence(answer: JsonObject) -> float | None:
    return _probability(answer.get("confidence"))
