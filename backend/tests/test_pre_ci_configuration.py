"""Regression tests for pre-CI dependency, workflow, and ignore configuration."""

import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent


def test_jsonschema_is_declared_in_dev_dependencies() -> None:
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text())
    dev_dependencies = config["project"]["optional-dependencies"]["dev"]
    assert "jsonschema>=4.26,<5" in dev_dependencies


def test_lockfile_contains_jsonschema() -> None:
    lock_text = (BACKEND_ROOT / "uv.lock").read_text()
    assert 'name = "jsonschema"' in lock_text
    assert 'version = "4.26.0"' in lock_text


def test_ci_uses_frozen_sync_and_required_root_qa_gates() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    assert "uv sync --frozen --all-extras" in workflow
    assert "backend/.venv/bin/python scripts/validate-evals.py" in workflow
    assert "${{ runner.temp }}/tool-contract-mismatches.ci.json" in workflow
    assert "backend/.venv/bin/python scripts/report-eval-coverage.py" in workflow
    assert "--profile chennai-pilot" in workflow
    assert workflow.count("working-directory: .") >= 2


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
