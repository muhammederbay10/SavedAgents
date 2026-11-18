"""Fakes, settings and a seeded set of saves for the retrieval tests.

Every name, place and caption is synthetic. Places are spread over several
countries on purpose, so nothing in search can depend on one city or language.
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import RetrievalPrompts
from app.agent.retrieval import HybridRetriever
from app.config import RetrievalSettings
from app.db.models import (
    AliasStatus,
    Entity,
    EntityAvailability,
    EntityStatus,
    ItemEntity,
    ItemStatus,
    SavedItem,
    User,
    UserEntity,
    Vocabulary,
    VocabularyAlias,
)
from app.db.types import GeoPoint
from app.models.base import (
    ChoiceAnswer,
    ChoiceQuestion,
    Classifier,
    ContentPart,
    DecisionResult,
    Embedder,
    EmbeddingResult,
    Question,
)
from app.models.errors import ModelError
from tests.database import TEST_EMBEDDING_DIMENSION
from tests.db_factories import unit_vector

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
TEST_PROMPTS = RetrievalPrompts(
    version="test-1",
    normalization_question="Which saved {filter} does the user mean?",
    normalization_no_match_option="None of these.",
    normalization_input="{filter}: {wording}",
)
NO_MATCH = "none"
PICK_PROBABILITY = 0.93
MIN_PROBABILITY = 0.6
AVAILABILITY_MAX_AGE_DAYS = 7.0


def retrieval_settings(**overrides: object) -> RetrievalSettings:
    values: dict[str, object] = {
        "search_time_budget_seconds": 10.0,
        "search_max_results": 20,
        "search_min_results": 1,
        "filter_normalization_min_probability": MIN_PROBABILITY,
        "filter_normalization_max_options": 20,
        "availability_max_age_days": AVAILABILITY_MAX_AGE_DAYS,
    }
    return RetrievalSettings(**{**values, **overrides})  # type: ignore[arg-type]


@dataclass
class PickingClassifier(Classifier):
    """Answers each normalization question by picking a scripted value among the options.

    `picks` maps `(filter, wording)` to the value to pick. Any other wording is
    answered "none of these". Every call's wording and options are recorded.
    """

    picks: Mapping[tuple[str, str], str] = field(default_factory=dict)
    probability: float = PICK_PROBABILITY
    error: ModelError | None = None
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    async def decide(
        self, state: Sequence[ContentPart], questions: Sequence[Question]
    ) -> DecisionResult:
        filter_name, wording = str(state[0]).split(": ", 1)
        question = questions[0]
        assert isinstance(question, ChoiceQuestion)
        offered = {key: value for key, value in question.options.items() if key != NO_MATCH}
        self.calls.append((wording, tuple(offered.values())))
        if self.error is not None:
            raise self.error
        wanted = self.picks.get((filter_name, wording))
        choice = next((key for key, value in offered.items() if value == wanted), NO_MATCH)
        answer = ChoiceAnswer(
            choice=choice, probabilities={choice: self.probability}, confidence=None
        )
        return DecisionResult(answers={question.key: answer}, model="fake", cost_usd=None)

    @property
    def offered_values(self) -> set[str]:
        return {value for _, options in self.calls for value in options}


@dataclass
class MappedEmbedder(Embedder):
    """Embeds known texts as chosen unit vectors, and anything else as a fixed far vector."""

    axes: Mapping[str, int] = field(default_factory=dict)
    error: ModelError | None = None
    dimension: int = TEST_EMBEDDING_DIMENSION
    delay_seconds: float = 0.0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        self.calls.append(tuple(texts))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        vectors = tuple(
            tuple(_unit(self.axes.get(text, self.dimension - 1), self.dimension)) for text in texts
        )
        return EmbeddingResult(vectors=vectors, model="fake/embedding", cost_usd=None)


def _unit(axis: int, dimension: int) -> list[float]:
    return [1.0 if index == axis else 0.0 for index in range(dimension)]


def build_retriever(
    *,
    classifier: Classifier | None = None,
    embedder: Embedder | None = None,
    settings: RetrievalSettings | None = None,
) -> HybridRetriever:
    return HybridRetriever(
        classifier=classifier or PickingClassifier(),
        embedder=embedder or MappedEmbedder(),
        prompts=TEST_PROMPTS,
        settings=settings or retrieval_settings(),
        now=lambda: NOW,
    )


@dataclass
class Saves:
    """The seeded users, items and entities, by a short name."""

    user: User
    other_user: User
    items: dict[str, SavedItem] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)

    def ids(self, *names: str) -> list[uuid.UUID]:
        return [self.items[name].id for name in names]


# Axes of the seeded item embeddings. The query embedder maps texts to these.
COFFEE_AXIS = 0
KOREAN_AXIS = 1
CAR_AXIS = 2
STUDY_AXIS = 3
HORROR_AXIS = 4

BEBEK = GeoPoint(latitude=41.0771, longitude=29.0431)
KARAKOY = GeoPoint(latitude=41.0225, longitude=28.9770)
BROOKLYN = GeoPoint(latitude=40.6782, longitude=-73.9442)
SHIBUYA = GeoPoint(latitude=35.6595, longitude=139.7005)


def _place(name: str, location: GeoPoint, **address: str) -> Entity:
    return Entity(
        type="place",
        source="google_places",
        external_id=f"synthetic-{name}",
        name=name,
        location=location,
        **address,
    )


def _movie(name: str, external_id: str, **data: object) -> Entity:
    return Entity(type="movie", source="tmdb", external_id=external_id, name=name, data=data)


async def seed_saves(session: AsyncSession) -> Saves:
    """Seed two users' saves, shared entities and a small vocabulary."""
    user = User(display_name="Asking User", country="US")
    other_user = User(display_name="Other User", country="JP")
    session.add_all([user, other_user])
    await session.flush()
    saves = Saves(user=user, other_user=other_user)
    saves.entities = {
        "kronotrop": _place(
            "Kronotrop",
            BEBEK,
            country="Türkiye",
            region="İstanbul",
            city="İstanbul",
            district="Beşiktaş",
            neighbourhood="Bebek",
        ),
        "kimchi": _place(
            "Kimchi House", KARAKOY, country="Türkiye", city="İstanbul", district="Beyoğlu"
        ),
        "diner": _place(
            "Corner Diner",
            BROOKLYN,
            country="United States",
            region="New York",
            city="New York",
            district="Brooklyn",
        ),
        "izakaya": _place("Night Izakaya", SHIBUYA, country="Japan", city="Tokyo"),
        "hereditary": _movie("Hereditary", "493922", genre=["horror"], year=2018),
        "conjuring": _movie("The Conjuring", "138843", genre=["horror"], year=2013),
        "inception": _movie("Inception", "27205", genre=["sci-fi"], year=2010),
    }
    session.add_all(saves.entities.values())
    await session.flush()
    _add_items(saves)
    session.add_all(saves.items.values())
    await session.flush()
    session.add_all(_links(saves))
    session.add_all(_availability(saves))
    session.add(
        UserEntity(
            user_id=user.id, entity_id=saves.entities["inception"].id, status=EntityStatus.DONE
        )
    )
    await _add_vocabulary(session)
    await session.flush()
    return saves


def _item(user: User, name: str, **values: object) -> SavedItem:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "platform": "instagram",
        "platform_post_id": f"synthetic-{user.id.hex[:8]}-{name}",
        "media_type": "reel",
        "status": ItemStatus.DONE,
    }
    return SavedItem(**{**defaults, **values})


def _add_items(saves: Saves) -> None:
    user, other = saves.user, saves.other_user
    day = timedelta(days=1)
    saves.items = {
        "kronotrop": _item(
            user,
            "kronotrop",
            type="place",
            category="café",
            tags=["coffee", "brunch"],
            collection="Food",
            fields={"cuisine": ["specialty coffee"]},
            summary="A cozy specialty coffee spot by the sea.",
            embedding=unit_vector(COFFEE_AXIS),
            saved_at=NOW - 6 * day,
        ),
        "kimchi": _item(
            user,
            "kimchi",
            type="place",
            category="restaurant",
            tags=["dinner"],
            collection="Food",
            fields={"cuisine": ["korean barbecue"]},
            summary="Korean barbecue with a long queue.",
            embedding=unit_vector(KOREAN_AXIS),
            saved_at=NOW - 5 * day,
        ),
        "diner": _item(
            user,
            "diner",
            type="place",
            category="restaurant",
            collection="Food",
            fields={"cuisine": ["american breakfast"]},
            summary="Pancakes and coffee all day.",
            saved_at=NOW - 4 * day,
        ),
        "izakaya": _item(
            user,
            "izakaya",
            type="place",
            category="bar",
            collection="Travel",
            summary="Small bar with grilled skewers.",
            saved_at=NOW - 3 * day,
        ),
        "hereditary": _item(
            user,
            "hereditary",
            type="movie",
            category="horror movie",
            collection="Movies",
            embedding=unit_vector(HORROR_AXIS),
            saved_at=NOW - 7 * day,
        ),
        "conjuring": _item(
            user,
            "conjuring",
            type="movie",
            category="horror movie",
            collection="Movies",
            saved_at=NOW - 8 * day,
        ),
        "inception": _item(
            user,
            "inception",
            type="movie",
            category="sci-fi movie",
            collection="Movies",
            saved_at=NOW - 9 * day,
        ),
        "integrals": _item(
            user,
            "integrals",
            type="study",
            category="maths tutorial",
            fields={"subject": "maths", "topic": "integrals by parts", "format": "tutorial"},
            summary="How to solve integrals by parts, step by step.",
            embedding=unit_vector(STUDY_AXIS),
            saved_at=NOW - 2 * day,
        ),
        "car": _item(
            user,
            "car",
            category="car review",
            tags=["bmw"],
            summary="A long test drive of a sports sedan.",
            embedding=unit_vector(CAR_AXIS),
            saved_at=NOW - 1 * day,
        ),
        "pending": _item(
            user,
            "pending",
            status=ItemStatus.PENDING,
            collection="Food",
            caption="Best lahmacun in Beşiktaş, cheap and fast",
            saved_at=NOW - 10 * day,
        ),
        "other_private": _item(
            other,
            "private",
            type="place",
            category="grandma ayşe's recipe",
            collection="Food",
            fields={"cuisine": ["grandma ayşe's cooking"]},
            saved_at=NOW - 1 * day,
        ),
        "other_place": _item(other, "place", type="place", category="café", collection="Food"),
    }


def _links(saves: Saves) -> list[ItemEntity]:
    pairs = [
        ("kronotrop", "kronotrop"),
        ("kimchi", "kimchi"),
        ("diner", "diner"),
        ("izakaya", "izakaya"),
        ("hereditary", "hereditary"),
        ("conjuring", "conjuring"),
        ("inception", "inception"),
        ("other_private", "kronotrop"),
        ("other_place", "diner"),
    ]
    return [
        ItemEntity(saved_item_id=saves.items[item].id, entity_id=saves.entities[entity].id)
        for item, entity in pairs
    ]


FRESH = NOW - timedelta(days=1)
STALE = NOW - timedelta(days=30)


def _availability(saves: Saves) -> list[EntityAvailability]:
    rows = [
        ("hereditary", "US", {"flatrate": ["Netflix"], "rent": ["Apple TV"]}, FRESH),
        ("hereditary", "JP", {"flatrate": ["U-NEXT"]}, FRESH),
        ("conjuring", "US", {"flatrate": ["Max"]}, STALE),
        ("conjuring", "JP", {"flatrate": ["Netflix"]}, FRESH),
        # Only Japan has a row: a US search must never use it.
        ("inception", "JP", {"flatrate": ["Netflix"]}, FRESH),
    ]
    return [
        EntityAvailability(
            entity_id=saves.entities[entity].id,
            country=country,
            providers=providers,
            refreshed_at=refreshed_at,
        )
        for entity, country, providers, refreshed_at in rows
    ]


async def _add_vocabulary(session: AsyncSession) -> None:
    restaurant = Vocabulary(
        field="category", value="restaurant", embedding=unit_vector(KOREAN_AXIS)
    )
    car_review = Vocabulary(field="category", value="car review", embedding=unit_vector(CAR_AXIS))
    session.add_all([restaurant, car_review])
    await session.flush()
    session.add_all(
        [
            VocabularyAlias(
                vocabulary_id=restaurant.id, alias="lokanta", status=AliasStatus.VERIFIED
            ),
            VocabularyAlias(
                vocabulary_id=restaurant.id, alias="restoran", status=AliasStatus.CANDIDATE
            ),
            VocabularyAlias(
                vocabulary_id=restaurant.id, alias="eatery", status=AliasStatus.REJECTED
            ),
        ]
    )
