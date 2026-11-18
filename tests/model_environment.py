"""The model settings used across tests, as environment variables.

The values are the approved model ids (decision record 0008) with dummy
endpoints and a dummy key. Nothing here can reach a real provider.
"""

MODEL_ENVIRONMENT = {
    "OPENROUTER_API_KEY": "synthetic-key-3b1f",
    "OPENROUTER_BASE_URL": "https://openrouter.test/api/v1",
    "OPENROUTER_DECISIONS_URL": "https://openrouter.test/api/alpha/decisions",
    "OPENROUTER_MAX_ATTEMPTS": "3",
    "OPENROUTER_RETRY_BASE_DELAY_SECONDS": "0.5",
    "OPENROUTER_RETRY_MAX_DELAY_SECONDS": "8",
    "DECISION_MODEL": "google/gemini-decision-1",
    "DECISION_TIMEOUT_SECONDS": "30",
    "TEXT_MODEL": "deepseek/deepseek-v4.1-flash",
    "TEXT_MODEL_PROVIDER": "coreweave/fp8",
    "TEXT_MODEL_PROVIDER_NAME": "CoreWeave",
    "TEXT_TIMEOUT_SECONDS": "30",
    "ESCALATION_MODEL": "google/gemini-2.5-flash",
    "ESCALATION_TIMEOUT_SECONDS": "90",
    "TRANSCRIPTION_MODEL": "microsoft/mai-transcribe-2",
    "TRANSCRIPTION_RESPONSE_FORMAT": "verbose_json",
    "TRANSCRIPTION_FALLBACK_MODEL": "openai/gpt-transcribe",
    "TRANSCRIPTION_FALLBACK_RESPONSE_FORMAT": "json",
    "TRANSCRIPTION_TIMEOUT_SECONDS": "60",
    "EMBEDDING_MODEL": "openai/text-embedding-3-large",
    "EMBEDDING_DIMENSION": "1024",
    "EMBEDDING_TIMEOUT_SECONDS": "30",
}
