"""Topology contract tests for single-node staging runtime.

Validates Compose topology, Dockerfile, and env template define all
required services with correct wiring, without running containers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
COMPOSE_PATH = REPO_ROOT / "docker-compose.staging.yml"
DOCKERFILE_PATH = BACKEND_ROOT / "Dockerfile"
ENV_TEMPLATE = REPO_ROOT / "docs" / "staging-env.template"

REQUIRED_SERVICES = {
    "postgres",
    "migrate",
    "backend",
    "inbound-worker",
    "notification-worker",
}

WORKER_ENTRYPOINTS = {
    "inbound-worker": "run_inbound_worker.py",
    "notification-worker": "run_worker.py",
}

APP_SERVICES = ("backend", "inbound-worker", "notification-worker")
ALL_APP_AND_MIGRATE = ("migrate", *APP_SERVICES)
WORKER_SERVICES = ("inbound-worker", "notification-worker")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


@pytest.fixture(scope="module")
def compose_raw() -> str:
    return COMPOSE_PATH.read_text()


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE_PATH.read_text()


class TestComposeTopology:
    def test_all_required_services_defined(self, compose: dict) -> None:
        services = set(compose.get("services", {}).keys())
        missing = REQUIRED_SERVICES - services
        assert not missing, f"Missing services: {missing}"

    def test_retention_worker_not_in_topology(self, compose: dict) -> None:
        assert "retention-worker" not in compose.get("services", {})

    def test_postgres_has_health_check(self, compose: dict) -> None:
        assert "healthcheck" in compose["services"]["postgres"]

    def test_migration_waits_for_healthy_postgres(self, compose: dict) -> None:
        deps = compose["services"]["migrate"].get("depends_on", {})
        assert deps.get("postgres", {}).get("condition") == "service_healthy"

    def test_app_services_wait_for_migration(self, compose: dict) -> None:
        for svc in APP_SERVICES:
            deps = compose["services"][svc].get("depends_on", {})
            assert "migrate" in deps, f"{svc} missing migrate dependency"
            assert deps["migrate"].get("condition") == "service_completed_successfully"

    def test_migration_does_not_restart(self, compose: dict) -> None:
        assert compose["services"]["migrate"].get("restart") in (None, "no", "on-failure")

    def test_workers_have_restart_policy(self, compose: dict) -> None:
        for svc in WORKER_SERVICES:
            restart = compose["services"][svc].get("restart")
            assert restart in ("unless-stopped", "on-failure", "always"), f"{svc} restart={restart}"

    def test_worker_commands_point_to_existing_entrypoints(self, compose: dict) -> None:
        for svc, entrypoint in WORKER_ENTRYPOINTS.items():
            cmd = compose["services"][svc].get("command", [])
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            assert entrypoint in cmd_str, f"{svc} command missing {entrypoint}"
            assert (BACKEND_ROOT / entrypoint).exists(), f"{entrypoint} not found"

    def test_all_app_services_use_same_image(self, compose: dict) -> None:
        images = {svc: compose["services"][svc].get("image") for svc in ALL_APP_AND_MIGRATE}
        unique = set(images.values())
        assert len(unique) == 1, f"Different images: {images}"
        assert None not in unique, "All app services must specify explicit image"

    def test_exactly_one_build_producer(self, compose: dict) -> None:
        builders = [name for name, svc in compose["services"].items() if "build" in svc]
        assert len(builders) == 1, f"Expected 1 build producer, got: {builders}"
        build_ctx = compose["services"][builders[0]]["build"]
        ctx_path = build_ctx if isinstance(build_ctx, str) else build_ctx.get("context", ".")
        assert (REPO_ROOT / ctx_path).is_dir(), f"Build context {ctx_path} not found"

    def test_no_source_bind_mounts(self, compose: dict) -> None:
        for name, svc in compose.get("services", {}).items():
            for vol in svc.get("volumes", []):
                if isinstance(vol, str) and ":" in vol:
                    host_path = vol.split(":")[0]
                    assert not host_path.startswith(("..", ".", "/")), (
                        f"{name} has source bind mount: {vol}"
                    )

    def test_no_literal_secrets(self, compose_raw: str) -> None:
        assert "changeme" not in compose_raw.lower()
        assert "password123" not in compose_raw.lower()
        assert "sk_" not in compose_raw

    def test_no_credential_urls_in_compose(self, compose_raw: str) -> None:
        for line in compose_raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "postgresql+asyncpg://" not in stripped, (
                f"Credential-bearing URL in Compose: {stripped}"
            )

    def test_database_not_publicly_exposed(self, compose: dict) -> None:
        for port in compose["services"]["postgres"].get("ports", []):
            assert "127.0.0.1" in str(port), f"PostgreSQL exposed publicly: {port}"

    def test_api_has_health_check(self, compose: dict) -> None:
        assert "healthcheck" in compose["services"]["backend"]

    def test_workers_disable_inherited_healthcheck(self, compose: dict) -> None:
        for svc in WORKER_SERVICES:
            hc = compose["services"][svc].get("healthcheck", {})
            assert hc.get("disable") is True, f"{svc} inherits API HEALTHCHECK on :8000"

    def test_inbound_worker_requires_only_its_own_vars(self, compose: dict) -> None:
        env = compose["services"]["inbound-worker"].get("environment", {})
        assert "SARVAM_API_KEY" in env
        assert "WHATSAPP_ACCESS_TOKEN" not in env
        assert "WHATSAPP_BUSINESS_MAPPINGS" not in env

    def test_notification_worker_requires_provider_vars(self, compose_raw: str) -> None:
        assert "WHATSAPP_ACCESS_TOKEN: ${WHATSAPP_ACCESS_TOKEN:?" in compose_raw
        assert "WHATSAPP_BUSINESS_MAPPINGS: ${WHATSAPP_BUSINESS_MAPPINGS:?" in compose_raw

    def test_backend_requires_webhook_security_vars(self, compose_raw: str) -> None:
        assert "WHATSAPP_VERIFY_TOKEN: ${WHATSAPP_VERIFY_TOKEN:?" in compose_raw
        assert "WHATSAPP_APP_SECRET: ${WHATSAPP_APP_SECRET:?" in compose_raw
        assert "WHATSAPP_BUSINESS_MAPPINGS: ${WHATSAPP_BUSINESS_MAPPINGS:?" in compose_raw

    def test_db_components_injected_to_all_app_services(self, compose: dict) -> None:
        for svc in ALL_APP_AND_MIGRATE:
            env = compose["services"][svc].get("environment", {})
            for var in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"):
                assert var in env, f"{svc} missing {var}"

    def test_no_database_url_in_compose(self, compose: dict) -> None:
        for name, svc in compose["services"].items():
            env = svc.get("environment", {})
            assert "DATABASE_URL" not in env, f"{name} has DATABASE_URL — use DB_* components"

    def test_pool_env_injected_to_all_app_services(self, compose: dict) -> None:
        for svc in APP_SERVICES:
            env = compose["services"][svc].get("environment", {})
            for var in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE"):
                assert var in env, f"{svc} missing {var}"

    def test_backend_api_port_bound_to_localhost(self, compose: dict) -> None:
        for port in compose["services"]["backend"].get("ports", []):
            assert "127.0.0.1" in str(port)

    def test_backend_stop_grace_covers_request_drain(self, compose: dict) -> None:
        grace = compose["services"]["backend"].get("stop_grace_period", "10s")
        seconds = int(grace.replace("s", ""))
        assert seconds >= 35, f"API stop_grace_period {seconds}s < 35s request drain margin"

    def test_all_workers_have_stop_grace_period(self, compose: dict) -> None:
        for svc in WORKER_SERVICES:
            grace = compose["services"][svc].get("stop_grace_period")
            assert grace is not None, f"{svc} missing stop_grace_period"

    def test_db_password_references_postgres_password(self, compose_raw: str) -> None:
        assert "DB_PASSWORD: ${POSTGRES_PASSWORD:?" in compose_raw


class TestEnvTemplate:
    def test_has_all_required_vars(self) -> None:
        template = ENV_TEMPLATE.read_text()
        for var in (
            "POSTGRES_PASSWORD",
            "INTERNAL_API_SECRET",
            "SARVAM_API_KEY",
            "WHATSAPP_ACCESS_TOKEN",
            "WHATSAPP_BUSINESS_MAPPINGS",
            "WHATSAPP_VERIFY_TOKEN",
            "WHATSAPP_APP_SECRET",
        ):
            assert var in template, f"Template missing {var}"

    def test_no_credential_urls(self) -> None:
        template = ENV_TEMPLATE.read_text()
        for line in template.splitlines():
            if line.startswith("#"):
                continue
            assert "://" not in line or line.strip().startswith("#"), (
                f"Credential URL in template: {line.strip()}"
            )

    def test_no_database_url_in_template(self) -> None:
        template = ENV_TEMPLATE.read_text()
        for line in template.splitlines():
            if line.startswith("#"):
                continue
            assert not line.startswith("DATABASE_URL="), "DATABASE_URL must not be in template"

    def test_no_real_secrets(self) -> None:
        template = ENV_TEMPLATE.read_text()
        assert "sk_" not in template
        for line in template.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip()
            if not val:
                continue
            safe = (
                "changeme" in val or val.startswith("{") or val in ("json", "INFO") or val.isdigit()
            )
            assert safe, f"Possible real secret in template: {key}={val}"
            assert not val.startswith("sk_"), f"Provider key prefix in template: {key}"
            assert not val.startswith("whsec_"), f"Webhook secret prefix in template: {key}"
            assert not val.startswith("EAA"), f"Meta token prefix in template: {key}"

    def test_whatsapp_mappings_direction(self) -> None:
        template = ENV_TEMPLATE.read_text()
        assert "phone_number_id" in template.lower()


class TestDockerfile:
    def test_locked_install(self, dockerfile: str) -> None:
        assert "uv sync --locked" in dockerfile

    def test_entrypoints_copied(self, dockerfile: str) -> None:
        for ep in ("run.py", "run_worker.py", "run_inbound_worker.py", "run_retention_worker.py"):
            assert ep in dockerfile, f"{ep} not copied in Dockerfile"

    def test_non_root_user(self, dockerfile: str) -> None:
        assert "USER fonely" in dockerfile or "USER 1000" in dockerfile

    def test_no_debug_reload(self, dockerfile: str) -> None:
        assert "--reload" not in dockerfile

    def test_migrations_copied(self, dockerfile: str) -> None:
        assert "migrations/" in dockerfile

    def test_exec_form_cmd(self, dockerfile: str) -> None:
        lines = dockerfile.splitlines()
        in_healthcheck = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("HEALTHCHECK"):
                in_healthcheck = True
                continue
            if in_healthcheck:
                in_healthcheck = False
                continue
            if stripped.startswith("CMD"):
                assert stripped.startswith("CMD ["), f"CMD not in exec form: {stripped}"


class TestDbUrlConstruction:
    def test_constructs_url_from_components(self) -> None:
        import os

        env_backup = {
            k: os.environ.pop(k, None)
            for k in (
                "DATABASE_URL",
                "DB_HOST",
                "DB_PORT",
                "DB_USER",
                "DB_PASSWORD",
                "DB_NAME",
            )
        }
        try:
            os.environ["DB_HOST"] = "testhost"
            os.environ["DB_PORT"] = "5433"
            os.environ["DB_USER"] = "testuser"
            os.environ["DB_PASSWORD"] = "p@ss"

            from fonely.core.config import Settings

            s = Settings()
            assert "testuser" in s.database_url
            assert "testhost" in s.database_url
            assert "5433" in s.database_url
            from sqlalchemy import make_url

            parsed = make_url(s.database_url)
            assert parsed.password == "p@ss"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_explicit_database_url_takes_precedence(self) -> None:
        import os

        env_backup = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "postgresql+asyncpg://explicit@h:5432/db"

            from fonely.core.config import Settings

            s = Settings()
            assert s.database_url == "postgresql+asyncpg://explicit@h:5432/db"
        finally:
            if env_backup is not None:
                os.environ["DATABASE_URL"] = env_backup
            else:
                os.environ.pop("DATABASE_URL", None)

    def test_no_env_preserves_default(self) -> None:
        import os

        env_backup = {
            k: os.environ.pop(k, None)
            for k in (
                "DATABASE_URL",
                "DB_HOST",
                "DB_PORT",
                "DB_USER",
                "DB_PASSWORD",
                "DB_NAME",
            )
        }
        try:
            from fonely.core.config import Settings

            s = Settings()
            assert s.database_url == "postgresql+asyncpg://localhost:5432/fonely"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_special_chars_roundtrip(self) -> None:
        import os

        env_backup = {
            k: os.environ.pop(k, None)
            for k in (
                "DATABASE_URL",
                "DB_HOST",
                "DB_PORT",
                "DB_USER",
                "DB_PASSWORD",
                "DB_NAME",
            )
        }
        try:
            os.environ["DB_PASSWORD"] = "p@ss:w0rd/+ñ %25"
            os.environ["DB_HOST"] = "myhost"

            from fonely.core.config import Settings

            s = Settings()
            from sqlalchemy import make_url

            parsed = make_url(s.database_url)
            assert parsed.password == "p@ss:w0rd/+ñ %25"
            assert parsed.host == "myhost"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_whitespace_password(self) -> None:
        import os

        env_backup = {
            k: os.environ.pop(k, None) for k in ("DATABASE_URL", "DB_HOST", "DB_PASSWORD")
        }
        try:
            os.environ["DB_PASSWORD"] = "pass word"
            os.environ["DB_HOST"] = "h"

            from fonely.core.config import Settings

            s = Settings()
            from sqlalchemy import make_url

            parsed = make_url(s.database_url)
            assert parsed.password == "pass word"
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_no_credential_url_in_entrypoints(self) -> None:
        for ep in ("run_inbound_worker.py", "run_worker.py", "run_retention_worker.py", "run.py"):
            path = BACKEND_ROOT / ep
            if not path.exists():
                continue
            content = path.read_text()
            assert "database_url" not in content.lower() or "settings.database_url" in content, (
                f"{ep} references database_url outside settings"
            )
