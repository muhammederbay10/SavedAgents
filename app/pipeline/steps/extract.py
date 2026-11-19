"""Step 4, extract: the text model fills in the type's schema, with a rare video escalation.

The text model reads the collection hint, caption and transcript and fills in
the schema the registry gives for the item's type, or the shared schema
(summary, category, tags) for an item with no built-in type. A list post gets
several entities.

**Escalation** (`docs/ARCHITECTURE.md` §3.3): when the type has a resolver,
so it needs an entity, and the text named none, the escalation model watches
the stored video or image itself (for example a silent reel whose only
information is a text overlay) and fills in the same schema. Its answer is used
when it names an entity. A reel shared by DM has no stored media, so it can't
be escalated.

A reply that doesn't follow the schema is an error, and the job is tried again.
Its content is never quoted in the error.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from pydantic import ValidationError

from app.models.base import (
    ContentPart,
    GenerationRequest,
    Generator,
    Image,
    Message,
    OutputSchema,
    Video,
)
from app.models.errors import ModelResponseError
from app.pipeline.costs import CallCosts
from app.pipeline.media.storage import MediaStore
from app.pipeline.prompts import PipelinePrompts
from app.pipeline.steps.fetch import MediaKind, StoredMedia
from app.types.base import EntityExtraction, Extraction
from app.types.registry import ContentTypeRegistry

logger = logging.getLogger(__name__)

_SCHEMA_NAME: Final = "saved_post_extraction"
_SECTION_BREAK: Final = "\n\n"


class Escalation(StrEnum):
    """Whether the escalation model was needed, and what it found."""

    NOT_NEEDED = "not_needed"
    NO_MEDIA = "no_media"
    FOUND = "found"
    NOT_FOUND = "not_found"

    @property
    def was_called(self) -> bool:
        """Return whether the escalation model was asked."""
        return self in {Escalation.FOUND, Escalation.NOT_FOUND}


class ExtractionOutputError(ModelResponseError):
    """The model's reply was not an object that follows the extraction schema."""


@dataclass(frozen=True)
class ExtractionResult:
    """What the extract step produced.

    Attributes:
        document: The extraction, checked against the schema, as JSON.
        escalation: Whether the escalation ran and what it found.
        text_model: The text model that answered.
        escalation_model: The escalation model that answered, if it was asked.
        costs: The charges of the text and escalation calls.
    """

    document: dict[str, object]
    escalation: Escalation
    text_model: str
    escalation_model: str | None
    costs: CallCosts

    def to_output(self, prompts_version: str) -> dict[str, object]:
        """Return the step's output for `enrichment_run.output`.

        Args:
            prompts_version: The version of the wording that was used.

        Returns:
            A JSON object. It holds the post's extracted content; it belongs
            to the item and is deleted with it.
        """
        return {
            "document": self.document,
            "escalation": self.escalation,
            "text_model": self.text_model,
            "escalation_model": self.escalation_model,
            "costs": self.costs.to_output(),
            "prompts_version": prompts_version,
        }

    @classmethod
    def from_output(cls, output: dict[str, object]) -> Self:
        """Read the step's output back, when a job continues after a retry.

        Args:
            output: What `to_output` returned.

        Returns:
            The extraction result.

        Raises:
            ValueError: If the output is not one `to_output` wrote.
        """
        document = output.get("document")
        if not isinstance(document, dict):
            raise ValueError("the extract step's output has no document")
        text_model = output.get("text_model")
        escalation_model = output.get("escalation_model")
        return cls(
            document=document,
            escalation=Escalation(str(output.get("escalation"))),
            text_model=text_model if isinstance(text_model, str) else "",
            escalation_model=escalation_model if isinstance(escalation_model, str) else None,
            costs=CallCosts.from_output(output.get("costs")),
        )


@dataclass(frozen=True)
class PostText:
    """The text the extraction reads.

    Attributes:
        caption: The post's caption, if any.
        collection: The user's collection hint, in their own words, if any.
        transcript: What was said, when the item has speech.
    """

    caption: str | None
    collection: str | None
    transcript: str | None


class Extractor:
    """Runs the text model with the type's schema, and the escalation when needed."""

    def __init__(
        self,
        text_generator: Generator,
        escalation_generator: Generator,
        registry: ContentTypeRegistry,
        prompts: PipelinePrompts,
        store: MediaStore,
        *,
        temperature: float,
        max_output_tokens: int,
    ) -> None:
        """Create the step.

        Args:
            text_generator: The text model.
            escalation_generator: The video model for escalations.
            registry: The built-in types and their schemas.
            prompts: The wording.
            store: Where the media is kept, for the escalation.
            temperature: Sampling temperature of both models.
            max_output_tokens: Upper limit on the length of their replies.
        """
        self._text_generator = text_generator
        self._escalation_generator = escalation_generator
        self._registry = registry
        self._prompts = prompts
        self._store = store
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    async def extract(
        self, type_name: str | None, post: PostText, media: StoredMedia
    ) -> ExtractionResult:
        """Extract an item's open category, tags, summary and type fields.

        Args:
            type_name: The built-in type, or `None` for an item without one.
            post: The text to read.
            media: What the fetch step kept, for the escalation.

        Returns:
            The checked extraction, and whether the escalation ran.

        Raises:
            ModelError: If a model call failed, or its reply didn't follow the schema.
            MediaNotFoundError: If the stored media for an escalation is gone.
        """
        schema = self._registry.extraction_schema(type_name)
        input_text = self._input_text(post)
        text_reply = await self._ask(
            self._text_generator, self._prompts.extraction_system, [input_text], schema
        )
        extraction, model, costs = text_reply
        if not _needs_escalation(self._registry, type_name, extraction):
            return ExtractionResult(
                document=extraction.model_dump(mode="json"),
                escalation=Escalation.NOT_NEEDED,
                text_model=model,
                escalation_model=None,
                costs=costs,
            )
        if media.kind is None or media.media_key is None:
            logger.info("extraction_escalation_without_media", extra={"type": type_name})
            return ExtractionResult(
                document=extraction.model_dump(mode="json"),
                escalation=Escalation.NO_MEDIA,
                text_model=model,
                escalation_model=None,
                costs=costs,
            )
        escalated, escalation_model, escalation_costs = await self._ask(
            self._escalation_generator,
            self._prompts.extraction_system + _SECTION_BREAK + self._prompts.escalation_system,
            [input_text, await self._media_part(media)],
            schema,
        )
        found = not _needs_escalation(self._registry, type_name, escalated)
        logger.info("extraction_escalated", extra={"type": type_name, "found_entity": found})
        return ExtractionResult(
            document=(escalated if found else extraction).model_dump(mode="json"),
            escalation=Escalation.FOUND if found else Escalation.NOT_FOUND,
            text_model=model,
            escalation_model=escalation_model,
            costs=costs + escalation_costs,
        )

    def _input_text(self, post: PostText) -> str:
        missing = self._prompts.missing_value
        return self._prompts.extraction_input.format(
            collection=post.collection or missing,
            caption=post.caption or missing,
            transcript=post.transcript or missing,
        )

    async def _media_part(self, media: StoredMedia) -> Video | Image:
        if media.media_key is None or media.mime_type is None:
            raise ValueError("the escalation needs stored media")
        data = await self._store.get(media.media_key)
        if media.kind is MediaKind.VIDEO:
            return Video(data=data, mime_type=media.mime_type)
        return Image(data=data, mime_type=media.mime_type)

    async def _ask(
        self,
        generator: Generator,
        system: str,
        content: list[ContentPart],
        schema: type[Extraction],
    ) -> tuple[Extraction, str, CallCosts]:
        result = await generator.generate(
            GenerationRequest(
                messages=[
                    Message(role="system", content=system),
                    Message(role="user", content=content),
                ],
                output_schema=OutputSchema(name=_SCHEMA_NAME, schema=schema.model_json_schema()),
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
            )
        )
        if result.structured is None:
            raise ExtractionOutputError(
                f"generate with {result.model} returned no JSON object "
                f"(finish reason {result.finish_reason})",
                operation="generate",
                model=result.model,
            )
        try:
            extraction = schema.model_validate(result.structured)
        except ValidationError as error:
            locations = ", ".join(".".join(map(str, issue["loc"])) for issue in error.errors())
            raise ExtractionOutputError(
                f"generate with {result.model} returned JSON that breaks the schema at: "
                f"{locations or 'the top level'}",
                operation="generate",
                model=result.model,
            ) from None
        return extraction, result.model, CallCosts().add(result.cost_usd)


def _needs_escalation(
    registry: ContentTypeRegistry, type_name: str | None, extraction: Extraction
) -> bool:
    if type_name is None or registry.get(type_name).resolver is None:
        return False
    return isinstance(extraction, EntityExtraction) and not extraction.entities
