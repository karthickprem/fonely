"""Regression tests for pre-CI dependency, workflow, and ignore configuration."""

import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent


def test_jsonschema_is_declared_in_dev_dependencies() -> None:
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text())
    dev_dependencies = config["project"]["optional-dependencies"]["dev"]
    assert "jsonschema>=4.26,<5" in dev_dependencies


def test_lockfile_contains_resolved_jsonschema_dependency() -> None:
    lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == "jsonschema")
    assert package["version"] == "4.26.0"

    fonely = next(item for item in lock["package"] if item["name"] == "fonely")
    requirements = fonely["metadata"]["requires-dist"]
    jsonschema_requirement = next(item for item in requirements if item["name"] == "jsonschema")
    assert jsonschema_requirement["specifier"] == ">=4.26,<5"
    assert jsonschema_requirement["marker"] == "extra == 'dev'"


def test_ci_uses_frozen_sync_and_required_root_qa_gates() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    assert "actions/cache@0400d5f644dc74513175e3cd8d07132dd4860809" in workflow
    assert "uv sync --frozen --all-extras" in workflow
    assert "backend/.venv/bin/python scripts/validate-evals.py" in workflow
    assert "${{ runner.temp }}/tool-contract-mismatches.ci.json" in workflow
    assert "backend/.venv/bin/python scripts/report-eval-coverage.py" in workflow
    assert "--profile chennai-pilot" in workflow
    assert ".venv/bin/pytest -m postgres -q" in workflow
    assert workflow.count("working-directory: .") >= 2


def test_postgres_async_engine_and_tests_share_session_loop() -> None:
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text())
    pytest_options = config["tool"]["pytest"]["ini_options"]
    assert pytest_options["asyncio_mode"] == "auto"
    assert pytest_options["asyncio_default_fixture_loop_scope"] == "session"
    assert pytest_options["asyncio_default_test_loop_scope"] == "session"

    fixtures = (BACKEND_ROOT / "tests" / "integration" / "postgres" / "conftest.py").read_text()
    assert '@pytest_asyncio.fixture(scope="session", loop_scope="session")' in fixtures
    assert '@pytest_asyncio.fixture(autouse=True, loop_scope="session")' in fixtures
    assert '@pytest_asyncio.fixture(loop_scope="session")' in fixtures
    assert "def event_loop(" not in fixtures


def test_root_gitignore_contains_required_recursive_rules() -> None:
    rules = set((PROJECT_ROOT / ".gitignore").read_text().splitlines())
    required = {
        "**/.env",
        "**/.env.*",
        "!**/.env.example",
        "**/.venv/",
        "**/__pycache__/",
        "**/*.py[cod]",
        "**/node_modules/",
        "**/*.db",
        "**/*.log",
        "**/test_output/",
        "**/voice_samples/",
        "**/evals/results/",
        "**/*.pem",
        "**/*.key",
        "**/.ssh/",
    }
    assert required <= rules
