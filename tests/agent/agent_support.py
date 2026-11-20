"""Builds the response agent on fake models and a test database.

The text model is a `FakeGenerator`: each test scripts the tool calls and the
final answer, so the agent's loop and the tools run for real, offline.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.agent import ResponseAgent
from app.agent.prompts import AGENT_PROMPTS
from app.agent.search.retrieval import HybridRetriever
from app.agent.tools import Toolbox
from app.config.agent import AgentSettings
from app.models.base import GenerationRequest, GenerationResult, ToolCall
from app.models.errors import ModelError
from app.models.fakes import FakeEmbedder, FakeGenerator
from app.pipeline.vocabulary.corrections import ItemCorrection
from app.pipeline.vocabulary.normalize import Normalizer
from app.types.registry import CONTENT_TYPES
from tests.agent.retrieval_support import build_retriever
from tests.database import TEST_EMBEDDING_DIMENSION
from tests.pipeline.harness import RULES
from tests.pipeline.support import TEST_PROMPTS as PIPELINE_PROMPTS
from tests.pipeline.support import ScriptedClassifier

TEXT_MODEL = "fake/text"
MAX_STEPS = 5
MAX_RESULTS = 5
REVIEW_MIN_CORRECTIONS = 2


def agent_settings(**overrides: int) -> AgentSettings:
    values = {
        "agent_max_steps": MAX_STEPS,
        "agent_max_results_per_answer": MAX_RESULTS,
        "agent_max_output_tokens": 400,
        "vocab_alias_review_min_corrections": REVIEW_MIN_CORRECTIONS,
    }
    return AgentSettings(_env_file=None, **{**values, **overrides})  # type: ignore[arg-type]


def tool_calls(*calls: tuple[str, Mapping[str, object]]) -> GenerationResult:
    """A model reply that only calls tools."""
    return GenerationResult(
        text="",
        structured=None,
        tool_calls=tuple(
            ToolCall(id=f"call-{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls)
        ),
        finish_reason="tool_calls",
        model=TEXT_MODEL,
    )


def final_answer(
    language: str,
    reply: str,
    item_ids: Sequence[uuid.UUID] = (),
    notice: str | None = None,
) -> GenerationResult:
    """A model reply with the final answer."""
    structured = {
        "language": language,
        "reply": reply,
        "notice": notice,
        "item_ids": [str(item_id) for item_id in item_ids],
    }
    return structured_reply(structured)


def structured_reply(structured: Mapping[str, object]) -> GenerationResult:
    return GenerationResult(
        text=json.dumps(structured, ensure_ascii=False),
        structured=structured,
        tool_calls=(),
        finish_reason="stop",
        model=TEXT_MODEL,
    )


def tool_results(request: GenerationRequest) -> list[dict[str, object]]:
    """The JSON of every tool result the model was shown in a request."""
    return [
        json.loads(str(message.content)) for message in request.messages if message.role == "tool"
    ]


@dataclass
class AgentUnderTest:
    agent: ResponseAgent
    generator: FakeGenerator
    classifier: ScriptedClassifier


def build_agent(
    sessions: async_sessionmaker[AsyncSession],
    script: Sequence[GenerationResult | ModelError],
    *,
    retriever: HybridRetriever | None = None,
    classifier: ScriptedClassifier | None = None,
    **settings: int,
) -> AgentUnderTest:
    """The agent with real tools on the test database and a scripted text model."""
    generator = FakeGenerator(script)
    vocabulary_classifier = classifier or ScriptedClassifier()
    normalizer = Normalizer(
        sessions,
        vocabulary_classifier,
        FakeEmbedder(dimensions=TEST_EMBEDDING_DIMENSION),
        PIPELINE_PROMPTS,
        RULES,
    )
    toolbox = Toolbox(
        sessions=sessions,
        retriever=retriever or build_retriever(),
        corrections=ItemCorrection(
            sessions,
            normalizer,
            CONTENT_TYPES,
            alias_min_probability=RULES.alias_min_probability,
            review_min_corrections=REVIEW_MIN_CORRECTIONS,
        ),
        registry=CONTENT_TYPES,
    )
    agent = ResponseAgent(
        generator=generator,
        toolbox=toolbox,
        prompts=AGENT_PROMPTS,
        settings=agent_settings(**settings),
    )
    return AgentUnderTest(agent=agent, generator=generator, classifier=vocabulary_classifier)
