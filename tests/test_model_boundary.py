"""Model code stays inside `app/models/` (ARCHITECTURE §3.9, AGENTS.md §3).

Two rules are checked on the source of every module under `app/`:

1. No model SDK is imported outside `app/models/`.
2. Outside `app/models/`, only the interfaces, the errors and the factory are
   imported from it. The clients and the HTTP transport are private to the
   package; the fakes are for tests.
"""

import ast
from pathlib import Path

import pytest

APP_DIRECTORY = Path(__file__).resolve().parent.parent / "app"
MODELS_DIRECTORY = APP_DIRECTORY / "models"
MODELS_PACKAGE = "app.models"

# Top-level import names of model SDKs. None of them is a dependency today; the
# list stops one from being added and used in the wrong place.
MODEL_SDK_MODULES = frozenset(
    {
        "anthropic",
        "cohere",
        "deepseek",
        "faster_whisper",
        "google.genai",
        "google.generativeai",
        "groq",
        "langchain",
        "langchain_core",
        "langchain_openai",
        "litellm",
        "mistralai",
        "openai",
        "openrouter",
        "together",
        "vertexai",
        "whisper",
    }
)

PUBLIC_MODEL_MODULES = frozenset({"app.models.base", "app.models.errors", "app.models.factory"})


def imported_modules(source: str) -> set[str]:
    """Return every module a source file imports, as dotted names."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def is_in_package(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def model_sdk_imports(source: str) -> set[str]:
    return {
        module
        for module in imported_modules(source)
        if any(is_in_package(module, sdk) for sdk in MODEL_SDK_MODULES)
    }


def private_model_imports(source: str) -> set[str]:
    return {
        module
        for module in imported_modules(source)
        if is_in_package(module, MODELS_PACKAGE)
        and not any(is_in_package(module, public) for public in PUBLIC_MODEL_MODULES)
    }


def sources_outside_models() -> list[Path]:
    return sorted(
        path for path in APP_DIRECTORY.rglob("*.py") if MODELS_DIRECTORY not in path.parents
    )


def test_there_is_application_code_outside_models_to_check() -> None:
    assert APP_DIRECTORY / "main.py" in sources_outside_models()
    assert not any(MODELS_DIRECTORY in path.parents for path in sources_outside_models())


@pytest.mark.parametrize("path", sources_outside_models(), ids=lambda path: path.name)
def test_no_model_sdk_is_imported_outside_models(path: Path) -> None:
    assert model_sdk_imports(path.read_text(encoding="utf-8")) == set()


@pytest.mark.parametrize("path", sources_outside_models(), ids=lambda path: path.name)
def test_only_the_interfaces_errors_and_factory_are_imported_from_models(path: Path) -> None:
    assert private_model_imports(path.read_text(encoding="utf-8")) == set()


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("import openai", {"openai"}),
        ("import openai.types.chat as chat", {"openai.types.chat"}),
        ("from openai import AsyncOpenAI", {"openai", "openai.AsyncOpenAI"}),
        ("from google import genai", {"google.genai"}),
        ("import google.generativeai as genai", {"google.generativeai"}),
        (
            "def load():\n    from faster_whisper import WhisperModel",
            {"faster_whisper", "faster_whisper.WhisperModel"},
        ),
        ("import httpx\nfrom google.protobuf import message\nimport openai_like", set()),
    ],
)
def test_sdk_check_finds_every_import_form(source: str, found: set[str]) -> None:
    assert model_sdk_imports(source) == found


@pytest.mark.parametrize(
    ("source", "found"),
    [
        (
            "from app.models.openrouter.transport import OpenRouterTransport",
            {
                "app.models.openrouter.transport",
                "app.models.openrouter.transport.OpenRouterTransport",
            },
        ),
        ("import app.models.clients.deepseek", {"app.models.clients.deepseek"}),
        (
            "from app.models.clients import decision",
            {"app.models.clients", "app.models.clients.decision"},
        ),
        (
            "from app.models.fakes import FakeEmbedder",
            {"app.models.fakes", "app.models.fakes.FakeEmbedder"},
        ),
        (
            "from app.models.base import Generator\n"
            "from app.models.errors import ModelError\n"
            "from app.models.factory import build_model_clients",
            set(),
        ),
    ],
)
def test_private_import_check_allows_only_the_public_modules(source: str, found: set[str]) -> None:
    assert private_model_imports(source) == found
