# Fonely

Fonely is building a multilingual AI front desk for Indian small businesses. The target product answers inbound calls, understands the caller in their language, invokes strictly validated public tools, commits business transactions through deterministic backend services, and notifies the owner.

## Current maturity

Fonely is **not production-ready or pilot-validated**.

The repository currently contains:

- A Python 3.12 backend foundation with SQLAlchemy, Alembic, PostgreSQL, and a deterministic `PendingAction` lifecycle.
- Migrations through `0003`.
- A synthetic adversarial evaluation corpus with 211 cases and 377 turns.
- PostgreSQL integration contracts and GitHub Actions CI under active correction.
- A JavaScript voice feasibility prototype that is not connected to the production backend transaction path.

See [Current Project Status](docs/STATUS.md) for the latest independently verified evidence and active blockers.

## Target product path

```text
Inbound call
→ speech-to-text
→ validated public tool request
→ PendingAction proposal
→ caller confirmation
→ deterministic transaction engine
→ PostgreSQL commit
→ spoken committed result
→ owner notification
```

The model is never the system of record and must never directly perform internal commit operations.

## Repository layout

```text
backend/          Python backend, ORM, migrations, domain/services, and tests
evals/            Synthetic evaluation corpus and versioned contracts
scripts/          Validation, migration, PostgreSQL, and repository-audit tooling
infra/            Local disposable PostgreSQL configuration
src/, public/     Voice feasibility prototype
docs/             Product plan, operating model, QA, testing, and historical logs
```

## Authoritative documents

- [Current status](docs/STATUS.md)
- [Product and engineering roadmap](docs/PLAN.md)
- [Team and operating model](docs/TEAM_AND_OPERATING_MODEL.md)
- [PostgreSQL testing guide](docs/testing/POSTGRESQL.md)
- [Evaluation framework](evals/README.md)
- [Testing strategy](docs/qa/TEST_STRATEGY.md)
- [Production-readiness checklist](docs/qa/PRODUCTION_READINESS_CHECKLIST.md)

Daily status files are append-only historical records. They may contain older counts and blockers that were accurate at the time; use `docs/STATUS.md` for current truth.

## Local backend checks

```bash
cd backend
uv sync --frozen --all-extras
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest -m "not postgres" -q
```

PostgreSQL tests are destructive and must only target an approved disposable local test database. Read [the PostgreSQL guide](docs/testing/POSTGRESQL.md) before running them.

## Verification language

Documentation uses these terms narrowly:

- **Implemented:** code exists.
- **Locally tested:** the named local command passed.
- **PostgreSQL-tested:** the behavior passed against a real disposable PostgreSQL instance.
- **CI-passed:** the cited GitHub Actions run completed successfully.
- **Provider-tested:** a named provider was exercised with recorded results.
- **Pilot-validated:** observed with consenting real businesses/callers under the pilot protocol.

One level never implies the next.
