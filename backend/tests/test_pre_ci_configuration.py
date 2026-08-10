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
    assert "-m postgres -q" in workflow
    assert "working-directory: ." in workflow


def test_ci_evidence_initializer_after_checkout() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    checkout_pos = workflow.index("actions/checkout@")
    init_pos = workflow.index("ci_evidence.orchestrator init")
    setup_pos = workflow.index("actions/setup-python@")
    assert checkout_pos < init_pos < setup_pos


def test_ci_evidence_orchestrator_wraps_gates() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    for phase in ("lint", "typecheck", "test_non_pg", "test_pg", "migration_upgrade"):
        assert f"--phase {phase}" in workflow


def test_ci_evidence_plugin_explicitly_loaded() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    assert "-p ci_evidence.pytest_plugin" in workflow


def test_ci_evidence_reconciler_always() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    reconcile_idx = workflow.index("ci_evidence.reconcile")
    pre_block = workflow[max(0, reconcile_idx - 200) : reconcile_idx]
    assert "always()" in pre_block


def test_ci_evidence_upload_always() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    upload_idx = workflow.index("upload-artifact@")
    pre_block = workflow[max(0, upload_idx - 200) : upload_idx]
    assert "always()" in pre_block


def test_ci_evidence_terminal_success_assertion() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    assert "terminal.json" in workflow
    assert "success" in workflow


def test_ci_evidence_acceptance_tests_in_ci() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    assert "test_ci_evidence_acceptance.py" in workflow


def test_ci_evidence_no_shell_terminal_truth() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "backend-ci.yml").read_text()
    assert "test-terminal-state.json" not in workflow
    assert "verifier_not_run" not in workflow


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
