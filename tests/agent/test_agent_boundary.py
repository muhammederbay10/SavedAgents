"""The response agent reaches the database only through `app/agent/tools.py` (ARCHITECTURE §3.5).

The agent's loop, its prompts and the answer delivery are checked on their
source: none of them may import the database layer, the search that queries
it, or the pipeline's database helpers. Only the tools may.
"""

from pathlib import Path

import pytest

from tests.models.test_model_boundary import imported_modules, is_in_package

AGENT_DIRECTORY = Path(__file__).resolve().parents[2] / "app" / "agent"
TOOLS_MODULE = "app.agent.tools"

# Modules that read or write the database, directly or through a helper.
DATABASE_PACKAGES = (
    "sqlalchemy",
    "asyncpg",
    "app.db",
    "app.agent.search",
    "app.ingestion",
    "app.pipeline",
)

# Everything the agent's own code runs, outside the tools and the search below them.
AGENT_MODULES = sorted(
    [
        AGENT_DIRECTORY / "agent.py",
        AGENT_DIRECTORY / "prompts.py",
        AGENT_DIRECTORY / "send.py",
        *(AGENT_DIRECTORY / "replies").glob("*.py"),
    ]
)


@pytest.mark.parametrize("module_path", AGENT_MODULES, ids=lambda path: path.name)
def test_agent_code_outside_the_tools_never_touches_the_database(module_path: Path) -> None:
    imported = imported_modules(module_path.read_text())

    reaching_the_database = sorted(
        module
        for module in imported
        if any(is_in_package(module, package) for package in DATABASE_PACKAGES)
    )

    assert reaching_the_database == []


def test_the_agent_gets_its_data_from_the_tools() -> None:
    imported = imported_modules((AGENT_DIRECTORY / "agent.py").read_text())

    assert TOOLS_MODULE in imported
