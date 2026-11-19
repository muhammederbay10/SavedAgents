"""Step 2, classify: one decision-model pass on the media for the built-in type and is-list.

The decision model looks at the stored video or image with the caption and the
user's collection hint, and answers two questions in one call
(`docs/ARCHITECTURE.md` §3.3):

- which built-in type the post is, or "none of these". The options are the
  registry's types and their descriptions; "none" is a normal answer, and the
  post keeps its open category (decision record 0005);
- whether the post lists several things.

It is not asked whether the post has speech: an audio track and the transcript
decide that (decision record 0008).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Self

from app.models.base import (
    ChoiceAnswer,
    ChoiceQuestion,
    Classifier,
    ContentPart,
    Unanswered,
    YesNoAnswer,
    YesNoQuestion,
)
from app.pipeline.costs import CallCosts
from app.pipeline.prompts import PipelinePrompts
from app.types.registry import ContentTypeRegistry

logger = logging.getLogger(__name__)

NONE_OF_THESE: Final = "none"
TYPE_QUESTION_KEY: Final = "content_type"
IS_LIST_QUESTION_KEY: Final = "is_list"


@dataclass(frozen=True)
class Classification:
    """The decision model's answers about one post.

    Attributes:
        type_name: The built-in type, or `None` for "none of these" and for a
            question the model left unanswered.
        type_probability: The probability of the chosen option; `None` when unanswered.
        type_unanswered: Why the model gave no type, when it gave none.
        is_list: Whether the post lists several things.
        is_list_probability: The probability of yes; `None` when unanswered.
        model: The model that answered.
        costs: The call's charge.
    """

    type_name: str | None
    type_probability: float | None
    type_unanswered: str | None
    is_list: bool
    is_list_probability: float | None
    model: str
    costs: CallCosts

    def to_output(self, prompts_version: str) -> dict[str, object]:
        """Return the step's output for `enrichment_run.output`.

        Args:
            prompts_version: The version of the wording that was used.

        Returns:
            A JSON object with every attribute and the wording's version.
        """
        return {
            "type": self.type_name,
            "type_probability": self.type_probability,
            "type_unanswered": self.type_unanswered,
            "is_list": self.is_list,
            "is_list_probability": self.is_list_probability,
            "model": self.model,
            "costs": self.costs.to_output(),
            "prompts_version": prompts_version,
        }

    @classmethod
    def from_output(cls, output: dict[str, object]) -> Self:
        """Read the step's output back, when a job continues after a retry.

        Args:
            output: What `to_output` returned.

        Returns:
            The classification.
        """
        type_name = output.get("type")
        type_probability = output.get("type_probability")
        unanswered = output.get("type_unanswered")
        is_list_probability = output.get("is_list_probability")
        model = output.get("model")
        return cls(
            type_name=type_name if isinstance(type_name, str) else None,
            type_probability=_float_or_none(type_probability),
            type_unanswered=unanswered if isinstance(unanswered, str) else None,
            is_list=output.get("is_list") is True,
            is_list_probability=_float_or_none(is_list_probability),
            model=model if isinstance(model, str) else "",
            costs=CallCosts.from_output(output.get("costs")),
        )


class PostClassifier:
    """Asks the decision model for a post's built-in type and whether it is a list."""

    def __init__(
        self,
        classifier: Classifier,
        registry: ContentTypeRegistry,
        prompts: PipelinePrompts,
        *,
        is_list_min_probability: float,
    ) -> None:
        """Create the step.

        Args:
            classifier: The decision model.
            registry: The built-in types to choose from.
            prompts: The wording.
            is_list_min_probability: A post is a list post from this probability of yes.

        Raises:
            ValueError: If a built-in type has the name of the "none" option.
        """
        names = {content_type.name for content_type in registry.all()}
        if NONE_OF_THESE in names:
            raise ValueError(f"no content type may be named {NONE_OF_THESE!r}")
        self._classifier = classifier
        self._registry = registry
        self._prompts = prompts
        self._is_list_min_probability = is_list_min_probability

    async def classify(
        self, media: Sequence[ContentPart], caption: str | None, collection: str | None
    ) -> Classification:
        """Classify one post in one decision call.

        Args:
            media: The stored video or image, or nothing when none is available.
            caption: The post's caption, if any.
            collection: The user's collection hint, in their own words, if any.

        Returns:
            The built-in type (or none) and whether the post is a list.

        Raises:
            ModelError: If the decision call failed.
        """
        text = self._prompts.classify_input.format(
            caption=caption or self._prompts.missing_value,
            collection=collection or self._prompts.missing_value,
        )
        result = await self._classifier.decide([*media, text], self._questions())
        type_answer = result.answers[TYPE_QUESTION_KEY]
        is_list_answer = result.answers[IS_LIST_QUESTION_KEY]
        classification = Classification(
            type_name=_chosen_type(type_answer),
            type_probability=_choice_probability(type_answer),
            type_unanswered=type_answer.reason if isinstance(type_answer, Unanswered) else None,
            is_list=isinstance(is_list_answer, YesNoAnswer)
            and is_list_answer.probability_yes >= self._is_list_min_probability,
            is_list_probability=(
                is_list_answer.probability_yes if isinstance(is_list_answer, YesNoAnswer) else None
            ),
            model=result.model,
            costs=CallCosts().add(result.cost_usd),
        )
        if classification.type_unanswered is not None:
            logger.warning(
                "classification_type_unanswered",
                extra={"reason": classification.type_unanswered},
            )
        return classification

    def _questions(self) -> list[ChoiceQuestion | YesNoQuestion]:
        options = {
            content_type.name: content_type.description for content_type in self._registry.all()
        }
        options[NONE_OF_THESE] = self._prompts.classify_none_option
        return [
            ChoiceQuestion(
                key=TYPE_QUESTION_KEY,
                instructions=self._prompts.classify_type_question,
                options=options,
            ),
            YesNoQuestion(
                key=IS_LIST_QUESTION_KEY, instructions=self._prompts.classify_is_list_question
            ),
        ]


def _chosen_type(answer: object) -> str | None:
    if not isinstance(answer, ChoiceAnswer) or answer.choice == NONE_OF_THESE:
        return None
    return answer.choice


def _choice_probability(answer: object) -> float | None:
    if not isinstance(answer, ChoiceAnswer):
        return None
    return answer.probabilities.get(answer.choice)


def _float_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
