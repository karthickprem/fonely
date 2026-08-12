"""Static regression contracts for deployment files that CI does not execute."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
BASE_COMPOSE_PATH = REPO_ROOT / "docker-compose.staging.yml"
PUBLIC_COMPOSE_PATH = REPO_ROOT / "docker-compose.public.yml"
ENV_TEMPLATE_PATH = REPO_ROOT / "docs" / "staging-env.template"
PUBLIC_DEPLOYMENT_PATH = REPO_ROOT / "deploy" / "PUBLIC_DEPLOYMENT.md"

_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::[^}]*)?}")
_COPY_LINE = re.compile(r"^COPY\s+(.+?)\s+\./$", re.MULTILINE)
_COMMAND_ENTRYPOINT = re.compile(r'^\s+command:\s+\[.*?"([^"]+\.py)"\]\s*$', re.MULTILINE)
_EXPECTED_COMMAND_ENTRYPOINTS = {
    "run_inbound_worker.py",
    "run_retention_worker.py",
    "run_worker.py",
}


def _service_block(path: Path, name: str) -> str:
    """Return one top-level Compose service block."""
    lines = path.read_text().splitlines()
    start = lines.index(f"  {name}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            end = index
            break
        if line.startswith("  ") and not line.startswith("    "):
            end = index
            break
    return "\n".join(lines[start:end])


def _template_keys() -> set[str]:
    keys: set[str] = set()
    for raw_line in ENV_TEMPLATE_PATH.read_text().splitlines():
        line = raw_line.strip().removeprefix("#").strip()
        if line and "=" in line:
            key = line.partition("=")[0].strip()
            if key.isidentifier() and key.upper() == key:
                keys.add(key)
    return keys


def _dockerfile_copied_entrypoints() -> set[str]:
    copied: set[str] = set()
    for sources in _COPY_LINE.findall((BACKEND_ROOT / "Dockerfile").read_text()):
        copied.update(source for source in sources.split() if source.endswith(".py"))
    return copied


def test_every_runtime_python_entrypoint_is_packaged() -> None:
    commands = set(_COMMAND_ENTRYPOINT.findall(BASE_COMPOSE_PATH.read_text()))
    assert commands == _EXPECTED_COMMAND_ENTRYPOINTS

    copied = _dockerfile_copied_entrypoints()
    assert _EXPECTED_COMMAND_ENTRYPOINTS | {"run.py"} <= copied
    dockerfile = (BACKEND_ROOT / "Dockerfile").read_text()
    assert 'CMD [".venv/bin/python", "run.py"]' in dockerfile
    assert dockerfile.count("uv sync --locked --no-dev") == 2
    assert "uv sync --frozen" not in dockerfile
    assert "uv pip install" not in dockerfile


def test_all_long_running_services_wait_for_migrations() -> None:
    for name in ("backend", "inbound-worker", "notification-worker", "retention-worker"):
        block = _service_block(BASE_COMPOSE_PATH, name)
        expected = "depends_on:\n      migrate:\n        condition: service_completed_successfully"
        assert expected in block


def test_retention_worker_has_no_false_http_healthcheck() -> None:
    retention = _service_block(BASE_COMPOSE_PATH, "retention-worker")

    assert 'command: [".venv/bin/python", "run_retention_worker.py"]' in retention
    assert "restart: unless-stopped" in retention
    assert "healthcheck:\n      disable: true" in retention


def test_backend_container_healthchecks_use_database_readiness() -> None:
    compose_backend = _service_block(BASE_COMPOSE_PATH, "backend")
    assert "/health/ready" in compose_backend
    assert "/health/live" not in compose_backend

    dockerfile = (BACKEND_ROOT / "Dockerfile").read_text()
    assert "localhost:8000/health/ready" in dockerfile
    assert "localhost:8000/health/live" not in dockerfile


def test_environment_template_covers_all_compose_references() -> None:
    referenced = _ENV_REFERENCE.findall(BASE_COMPOSE_PATH.read_text())
    referenced.extend(_ENV_REFERENCE.findall(PUBLIC_COMPOSE_PATH.read_text()))

    assert set(referenced) <= _template_keys()


def test_public_registration_uses_private_loopback() -> None:
    runbook = PUBLIC_DEPLOYMENT_PATH.read_text()

    assert "http://127.0.0.1:8000/internal/v1/businesses/channel-identity" in runbook
    assert "https://api.example.in/internal/" not in runbook
    assert "public edge intentionally returns 404 for `/internal/*`" in runbook


def test_caddy_healthcheck_uses_public_host_header() -> None:
    caddy = _service_block(PUBLIC_COMPOSE_PATH, "caddy")

    assert "Host: $${FONELY_PUBLIC_DOMAIN}" in caddy
    assert "http://127.0.0.1/health/live" in caddy
    assert "http://localhost:80/health/live" not in caddy


def test_public_preflight_requires_selected_capabilities() -> None:
    runbook = PUBLIC_DEPLOYMENT_PATH.read_text()

    for capability in ("whatsapp", "exotel", "internal"):
        assert f"--require {capability}" in runbook
    assert "missing or placeholder gate is a failure" in runbook
