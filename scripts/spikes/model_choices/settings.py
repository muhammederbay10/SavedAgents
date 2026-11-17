"""Fixed values of the SAV-7 spike: endpoints, models, prices and the test set.

These are the spike's own constants. The application's model settings are added
to `app/config.py` by SAV-10.
"""

from dataclasses import dataclass
from typing import Literal

ResponseFormat = Literal["json", "verbose_json"]

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TRANSCRIPTIONS_PATH = "/audio/transcriptions"
EMBEDDINGS_PATH = "/embeddings"
KEY_PATH = "/key"
API_KEY_ENVIRONMENT_VARIABLE = "OPENROUTER_API_KEY"
REQUEST_TIMEOUT_SECONDS = 120.0

# Muhammed's limit for all SAV-7 live calls together, in US dollars.
SPEND_CAP_USD = 2.00
# Catalog prices can lag behind routing, so every estimate is doubled.
ESTIMATE_SAFETY_FACTOR = 2.0

# The dataset revision, sentence ids and hashes are in `fleurs_provenance.json`.
FLEURS_MANIFEST_NAME = "manifest.json"

# Japanese has no spaces between words, so it is scored per character (CER).
CHARACTER_METRIC_LANGUAGES = frozenset({"ja_jp"})
# Turkish lower-cases "I" to a dotless i and the dotted capital to "i"; other languages do not.
TURKISH_CASING_LANGUAGES = frozenset({"tr_tr"})

AUDIO_FORMAT = "wav"
TIMESTAMP_GRANULARITIES: tuple[str, ...] = ("word", "segment")
SECONDS_PER_MINUTE = 60.0
TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class SpeechToTextModel:
    """One speech-to-text model in the comparison.

    Attributes:
        model_id: The exact OpenRouter model id.
        response_format: The richest format the model accepted in round 2.
        catalog_usd_per_minute: The catalog price on 17 Nov 2026, for estimates only.
    """

    model_id: str
    response_format: ResponseFormat
    catalog_usd_per_minute: float


@dataclass(frozen=True)
class EmbeddingModel:
    """One embedding model in the comparison.

    Attributes:
        model_id: The exact OpenRouter model id.
        full_dimensions: The vector size when no `dimensions` value is sent.
        catalog_usd_per_million_tokens: The catalog price on 17 Nov 2026, for estimates only.
    """

    model_id: str
    full_dimensions: int
    catalog_usd_per_million_tokens: float


SPEECH_TO_TEXT_MODELS: tuple[SpeechToTextModel, ...] = (
    SpeechToTextModel("x-ai/grok-stt-2.0", "json", 0.10 / 60),
    SpeechToTextModel("microsoft/mai-transcribe-2", "verbose_json", 0.10 / 60),
    SpeechToTextModel("openai/gpt-transcribe", "json", 0.0045),
    SpeechToTextModel("meta/muse-voice-transcribe-1.0", "json", 0.003),
)

EMBEDDING_MODELS: tuple[EmbeddingModel, ...] = (
    EmbeddingModel("openai/text-embedding-3-large", 3072, 0.13),
    EmbeddingModel("qwen/qwen3-embedding-8b", 4096, 0.01),
)

# The size Muhammed approved and SAV-9 stores (`EMBEDDING_DIMENSION=1024`).
REDUCED_EMBEDDING_DIMENSIONS = 1024
