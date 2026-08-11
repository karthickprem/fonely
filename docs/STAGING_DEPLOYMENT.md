# Local and Host-Ready Deployment Contract

This guide starts Fonely's internal deployment topology for synthetic testing. It proves that the tracked services can be assembled; it does **not** prove that a hosted staging environment, provider account, real phone call, production backup schedule, or pilot is working.

For the separately owned public TLS/provider edge, see `deploy/PUBLIC_DEPLOYMENT.md` and overlay `docker-compose.public.yml`. Do not expose the base backend port directly to the internet.

## Command evidence status

Every command below is labelled with its evidence on this exact package:

- **EXECUTED (source/harness):** both proof-script `--help` commands were executed and confirmed the documented `--provision` and `--business-id` arguments. The deployment-contract, staging-readiness, and retention unit tests were executed outside this runbook.
- **EXECUTED (baseline host behavior):** on integration SHA `a6cc0a7`, the CEO ran the same `run.py` used by the image command and observed Uvicorn complete startup on `127.0.0.1:8098`. The CEO also executed the healthcheck's identical `urllib` request against `/health/ready` and observed HTTP 200 with `{"status":"ready"}`. This proves the command target and endpoint on the host, not the image.
- **EXECUTED (baseline functional proof):** on integration SHA `a6cc0a7`, the CEO ran a directly hosted backend against isolated database `fonely_test_ceo_mig` at migration `0015`, then executed the seed and booking-proof commands successfully. This is baseline evidence, not evidence that the container commands below work.
- **NOT EXECUTED:** Docker/Compose rendering, image build/start, worker logs, container networking, stop/reset, and container migration commands below. The base image, locked dependency layer, non-root filesystem permissions, in-container binding, and Compose service wiring remain unproven because Docker is absent on the verification host.

Do not reinterpret a command's presence in this guide as evidence that it ran.

## What the base topology runs

- PostgreSQL 16
- One-shot Alembic migration
- FastAPI backend
- Durable inbound worker
- Notification worker
- Retention worker, every six hours

The backend container is healthy only when `/health/ready` can reach PostgreSQL. `/health/live` remains the shallow process probe used by the public edge.

## Prerequisites

- Docker and Docker Compose
- No real customer data or production credentials
- A completed `.env.staging` based on the tracked template

## Configure and validate — NOT EXECUTED

```bash
cp docs/staging-env.template .env.staging
# Replace every active changeme value with a non-production test credential.

docker compose \
  -f docker-compose.staging.yml \
  --env-file .env.staging \
  config --quiet
```

The application reads `DATABASE_URL`, `LOG_FORMAT`, and other setting names directly—there is no `FONELY_` prefix. A misspelled variable may be ignored and fall back to a default, so verify the rendered Compose configuration rather than assuming the environment was consumed.

## Start the topology — NOT EXECUTED

```bash
docker compose \
  -f docker-compose.staging.yml \
  --env-file .env.staging \
  up -d --build

docker compose -f docker-compose.staging.yml --env-file .env.staging ps
```

Verify the private API from the host:

```bash
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

A successful local response proves only this running topology. It is not hosted-staging or provider evidence.

## Prove a synthetic booking — EXECUTED ON BASELINE, NOT IN CONTAINERS

Health checks do not exercise trusted identity, onboarding, booking transactions, idempotency, or overlap prevention. The CEO executed this proof on integration SHA `a6cc0a7` using a directly hosted backend and isolated PostgreSQL database; the containerized form remains not executed.

The server and scripts intentionally read different secret names. Set both to the same test value: the application reads `INTERNAL_API_SECRET`, while the proof scripts read `FONELY_INTERNAL_API_SECRET`. Setting only the latter leaves the internal routes unmounted.

Booking confirmation also requires `WHATSAPP_BUSINESS_MAPPINGS`, a JSON `{phone_number_id: business_id}` map. `WHATSAPP_PHONE_NUMBER_ID` alone is insufficient. A missing mapping fails safe with `503 whatsapp_mapping_missing` and rolls back the appointment.

From a prepared host development environment, seed only the synthetic demo clinic and run the functional proof:

```bash
export INTERNAL_API_SECRET='<same non-production test secret used by the server>'
export FONELY_INTERNAL_API_SECRET="$INTERNAL_API_SECRET"
HOST_DATABASE_URL='postgresql+asyncpg://fonely:<url-encoded-password>@127.0.0.1:5432/fonely'

backend/.venv/bin/python scripts/seed-demo-clinic.py \
  --base-url http://127.0.0.1:8000 \
  --database-url "$HOST_DATABASE_URL" \
  --provision

backend/.venv/bin/python scripts/prove-booking.py \
  --base-url http://127.0.0.1:8000 \
  --database-url "$HOST_DATABASE_URL" \
  --business-id <seeded-business-id>
```

Observed baseline result: provisioning created the business and owner through `POST /internal/v1/businesses`, then activated services, resources, eligibility, and split-shift schedules through the real onboarding lifecycle. The booking proof refused a closed-hours slot, proposed and committed an open-hours appointment with ₹300 and `Asia/Kolkata` facts, returned the same single appointment on idempotent retry, and rejected a second patient at the same resource/time. A direct PostgreSQL check found one confirmed appointment and one notification manifest in the named isolated database.

It is synthetic backend HTTP → service → PostgreSQL evidence with fictional clinic/patients. It is not WhatsApp-provider, voice, audio, Exotel, native-language, pilot, or container evidence.

## Inspect workers and retention — NOT EXECUTED

```bash
docker compose \
  -f docker-compose.staging.yml \
  --env-file .env.staging \
  logs -f backend inbound-worker notification-worker retention-worker
```

The retention worker uses the current defaults unless the corresponding `RETENTION_*_DAYS` variables are set. A successful cycle logs `retention_cleanup_complete` with per-category counts. Its presence in Compose proves deployment wiring; cleanup behavior is separately covered by PostgreSQL tests. A real retention operation additionally needs alerting and operator review, which this topology does not provide.

## Run database readiness verification — NOT EXECUTED

The verifier is a repository tool, not part of the runtime image. Run it from a prepared host development environment against the loopback-published database. Set the URL explicitly: Compose's `--env-file` does not export variables to the invoking shell, and the Compose hostname `postgres` is not resolvable from the host.


```bash
FONELY_READINESS_DATABASE_URL='postgresql+asyncpg://fonely:<url-encoded-password>@127.0.0.1:5432/fonely' \
FONELY_READINESS_ENVIRONMENT=local \
backend/.venv/bin/python scripts/check-deployment-readiness.py
```

Before trusting a migration command, verify the target database directly. `Settings` reads `DATABASE_URL`; exporting `FONELY_DATABASE_URL` does not select the database.

Do not pipe a gate command through `tail` or another process when its exit code matters. Redirect output to a file, preserve the command's own return code, and inspect the file afterward.

## Stop, reset, and application rollback — NOT EXECUTED

Stop the topology while preserving the database volume:

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging down
```

Delete the local database volume only when the data is disposable:

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging down -v
```

`POSTGRES_PASSWORD` initializes a new volume; changing it later does not rotate the password stored in an existing cluster. For a persistent volume, change the role password through an authenticated PostgreSQL administration session, update the URL atomically, and restart dependent services. For disposable synthetic data, remove the volume and initialize it again instead.

These commands are **not an application rollback**. A release rollback requires the prior immutable image identifier, migration compatibility assessment, and a tested rollback-or-forward-fix procedure. No such hosted release is proven by this guide.

## Migrations — NOT EXECUTED

Migrations run once before long-running processes start. To invoke the migration service manually:

```bash
docker compose \
  -f docker-compose.staging.yml \
  --env-file .env.staging \
  run --rm migrate
```

## Capacity guardrail

Do **not** start a second full API/worker replica set with this topology. One set can theoretically consume 55 of PostgreSQL's expected default 100 connections: API 10, plus three worker engines at SQLAlchemy's default 15 each. A second set reaches 110 before migration, health, operator, autovacuum, or reserved sessions. Make worker pool controls real, set an explicit database connection budget, and execute load evidence before adding replicas.

This is configuration-derived, not runtime-measured capacity evidence.

## Troubleshooting

| Issue | Check |
|---|---|
| Compose interpolation fails | Every active required key in `.env.staging` has a value |
| Backend is unhealthy | PostgreSQL health, migration exit status, and `/health/ready` logs |
| A worker exits | Migration completion, `DATABASE_URL`, and that worker's required provider settings |
| Retention does not run | `retention-worker` is present in `docker compose ps` and its logs show startup/cycle events |
| Connection refused from another host | Expected: base ports bind to `127.0.0.1`; use the reviewed public overlay for provider ingress |
| Migration targets the wrong database | Export `DATABASE_URL`, then query `current_database()` before destructive gates |

## Evidence boundaries

This configuration can establish only local or host-ready deployment evidence. Report the following independently:

- Docker/Compose rendering and image build
- Local process and readiness checks
- PostgreSQL and migration tests
- Disposable backup/restore verification
- Hosted DNS/TLS and public-edge checks
- Real WhatsApp and Exotel provider callbacks
- Real audio through STT, conversation, booking, and TTS
- Native-language review, load/soak, pilot, and production operations

A skipped, unavailable, or deferred item is **not run**, never passed.

## Prohibited uses

- Do not operate this mutable local-build topology as production.
- Do not use real customer data, production phone numbers, or live provider destinations in it.
- Do not expose port 8000 directly to the public internet.
- Do not commit `.env.staging` or credentials.
- Do not call this production-ready or hosted-staging validated solely because Compose starts.
