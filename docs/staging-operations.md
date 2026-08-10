# Fonely Single-Node Staging Operations

**Evidence level:** implemented, CI-verified static topology. Docker runtime validation unexecuted.
**NOT proven:** Docker build/smoke, public TLS, real Meta/Exotel, monitoring, load/soak, pilot, production.

## Scope

One PostgreSQL instance, one API, two workers (inbound, notification), migration gate. All services run from one backend image built via Compose.

Retention worker entrypoint exists but is NOT deployed in this topology pending a separate tenant-scoped, evidence-preserving correction.

## Prerequisites

- Docker with Compose v2 (or Podman with compose)
- `.env.staging` configured from `docs/staging-env.template`
- No real customer data or production credentials

## Configuration

Copy `docs/staging-env.template` to `.env.staging`. Required variables:

| Variable | Purpose | Required by |
|---|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL password (single source of truth) | postgres, all app services (via DB_PASSWORD) |
| `INTERNAL_API_SECRET` | Bearer token for internal API | backend |
| `SARVAM_API_KEY` | LLM gateway key | inbound-worker |
| `WHATSAPP_ACCESS_TOKEN` | Meta API token | notification-worker |
| `WHATSAPP_BUSINESS_MAPPINGS` | JSON `{"phone_number_id": business_id}` | backend, notification-worker |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token | backend |
| `WHATSAPP_APP_SECRET` | Webhook signature validation | backend |

Optional:
| `WHATSAPP_PHONE_NUMBER_ID` | Single-business convenience (not authority) | backend |

Database connection is constructed at runtime from `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_PORT` via SQLAlchemy `URL.create` — no credential-bearing URL is checked in or configured manually. `DB_PASSWORD` references `POSTGRES_PASSWORD` in Compose so there is one source of truth. When no DB component vars are set, the existing default `postgresql+asyncpg://localhost:5432/fonely` is preserved.

Worker drain deadline is configurable via `SHUTDOWN_TIMEOUT_SECONDS` (default 10s). Compose `stop_grace_period` must exceed this value plus cleanup margin.

Workers and the API fail closed at startup without their required credentials. This is intentional.

`WHATSAPP_BUSINESS_MAPPINGS` maps phone_number_id (string key) to business_id (integer value): `{"12345678": 1}`. Invalid JSON, wrong types, empty objects, or duplicate entries fail startup.

## Commands

All Compose commands must include `--env-file .env.staging`:

### Build and start

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d --build
```

The `backend` service has the sole `build:` stanza; all other app services reference the same `fonely-backend:local` image tag.

### Check service health

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging ps
curl --fail -s http://127.0.0.1:8000/health/live
curl --fail -s http://127.0.0.1:8000/health/ready
```

### View logs

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging logs -f --tail=50
docker compose -f docker-compose.staging.yml --env-file .env.staging logs inbound-worker --tail=20
```

### Graceful stop

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging stop
```

Workers receive SIGTERM, stop claiming new work, drain current unit within the configured deadline (default 10s), then close DB/HTTP. Docker sends SIGKILL after stop_grace_period if not exited.

### Full teardown (preserves data volume)

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging down
```

### Full teardown including data

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging down -v
```

## Service topology

| Service | Command | Depends on | Restart | Health |
|---|---|---|---|---|
| postgres | postgres:16-alpine | — | unless-stopped | pg_isready |
| migrate | alembic upgrade head | postgres healthy | no | one-shot |
| backend | run.py (uvicorn) | migrate completed | unless-stopped | /health/live |
| inbound-worker | run_inbound_worker.py | migrate completed | unless-stopped | disabled (no HTTP) |
| notification-worker | run_worker.py | migrate completed | unless-stopped | disabled (no HTTP) |

Worker healthchecks are disabled because workers do not serve HTTP. The Dockerfile HEALTHCHECK targets the API on :8000; workers explicitly override it with `disable: true`. Fatal worker errors exit the PID; Compose restart policy recovers the process. Wedged worker loops (non-fatal infinite retry) are not detected — this is a known limitation.

## Startup sequence

1. PostgreSQL starts and becomes healthy (pg_isready)
2. Migration service runs `alembic upgrade head` and exits
3. API and all workers start only after migration succeeds
4. Migration failure blocks all dependent services

## Migration failure recovery

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging logs migrate
docker compose -f docker-compose.staging.yml --env-file .env.staging down
# Fix the migration issue
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d --build
```

The migration service does not restart on failure. Fix the issue and redeploy.

## Worker failure diagnosis

```bash
docker compose -f docker-compose.staging.yml --env-file .env.staging ps
docker compose -f docker-compose.staging.yml --env-file .env.staging logs notification-worker --tail=50
```

Common startup failures:
- `WHATSAPP_ACCESS_TOKEN is required` — set in .env.staging
- `SARVAM_API_KEY is required` — set in .env.staging
- Connection refused — check PostgreSQL is running

Workers restart automatically on crash (unless-stopped policy). Configuration failures restart repeatedly — check logs and fix the configuration.

## Stale claim / worker crash recovery

Workers use lease-based claiming. If a worker crashes mid-processing:

1. The in-flight event's lease expires after the configured timeout
2. A restarted worker reclaims expired leases automatically
3. No manual database intervention required for normal restart

## Backup and restore

```bash
# Stop writers first
docker compose -f docker-compose.staging.yml --env-file .env.staging stop backend inbound-worker notification-worker

# Backup
docker compose -f docker-compose.staging.yml --env-file .env.staging exec postgres \
  pg_dump -U fonely fonely > backup.sql
test -s backup.sql || { echo "ERROR: backup is empty"; exit 1; }

# Drop and recreate schema for clean restore target
docker compose -f docker-compose.staging.yml --env-file .env.staging exec postgres \
  psql -U fonely -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT USAGE, CREATE ON SCHEMA public TO fonely;" fonely

# Restore into clean schema (ON_ERROR_STOP ensures visibility)
docker compose -f docker-compose.staging.yml --env-file .env.staging exec -T postgres \
  psql -U fonely -v ON_ERROR_STOP=1 -1 fonely < backup.sql

# Verify migration state after restore (fresh one-shot)
docker compose -f docker-compose.staging.yml --env-file .env.staging run --rm migrate

# Restart services and verify readiness
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d
curl --fail -s http://127.0.0.1:8000/health/ready || echo "WARN: readiness check failed after restore"
```

## Resource bounds

| Parameter | Default | Config env var | Verified |
|---|---|---|---|
| DB pool size (per service) | 5 | `DB_POOL_SIZE` | yes |
| DB pool overflow | 5 | `DB_MAX_OVERFLOW` | yes |
| DB pool timeout | 30s | `DB_POOL_TIMEOUT` | yes |
| DB pool recycle | 1800s | `DB_POOL_RECYCLE` | yes |
| Total DB connections (3 long-lived services) | ~30 max | 3×(pool+overflow) | calculated |
| Inbound worker poll interval | 2s | hardcoded | limitation |
| Shutdown drain timeout | 10s | `SHUTDOWN_TIMEOUT_SECONDS` | yes |
| Compose stop grace (workers) | 15s | stop_grace_period | yes |
| Compose stop grace (API) | 35s | stop_grace_period | yes |

Provider timeouts (Sarvam LLM, WhatsApp API) are fixed in their respective client libraries, not configurable via environment variables in this topology. This is a known limitation.

## Known blockers

- **Docker runtime validation** — unexecuted. Build, smoke, health, restart, graceful shutdown not proven.
- **Database role separation** — all services currently run as the PostgreSQL bootstrap owner. Correct separation into bootstrap/migration/runtime-app roles with least-privilege grants is required before production. This cannot be safely implemented or tested without Docker in this bounded pass.
- **Automated data retention** — retention worker entrypoint exists but is not deployed. Current DataRetentionService performs global SELECT/DELETE without trusted business_id scoping. Requires separate tenant-scoped, evidence-preserving correction before activation.

## Known limitations

- No public TLS ingress — requires cloudflared tunnel or reverse proxy
- No Exotel/voice integration — not on current main
- No monitoring/alerting stack
- No load/soak testing
- No multi-worker scaling
- Worker liveness is process-exit only — fatal errors exit PID (Compose restarts); wedged loops are not detected
- Inbound poll interval is not configurable without code change
- Provider call timeouts are library-fixed, not environment-configurable
