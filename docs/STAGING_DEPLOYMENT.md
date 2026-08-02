# Staging Deployment

Deploy Fonely's backend for internal testing with real appointment flows.

## Prerequisites

- Docker and Docker Compose
- PostgreSQL 16 (provided by Docker Compose)
- No real customer data or production credentials

## Quick start

```bash
# 1. Configure
cp .env.staging.template .env.staging
# Edit .env.staging — set real passwords and secrets

# 2. Start everything (PostgreSQL → migrate → backend)
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d

# 3. Verify
curl http://localhost:8000/health/live    # → {"status":"ok"}
curl http://localhost:8000/health/ready   # → {"status":"ready"}
```

## Test an appointment proposal

```bash
curl -X POST http://localhost:8000/internal/appointments/propose \
  -H "Content-Type: application/json" \
  -H "X-Internal-Secret: <your-secret>" \
  -d '{...}'
```

## View logs

```bash
docker compose -f docker-compose.staging.yml logs -f backend
```

Logs are structured JSON by default (`FONELY_LOG_FORMAT=json`). Set `FONELY_LOG_FORMAT=text` for human-readable output during debugging.

## Restart

```bash
docker compose -f docker-compose.staging.yml restart backend
```

## Rollback

```bash
# Stop and remove containers (data persists in volume)
docker compose -f docker-compose.staging.yml down

# To also remove database volume:
docker compose -f docker-compose.staging.yml down -v
```

## Migration

Migrations run automatically as an init service before the backend starts. To run manually:

```bash
docker compose -f docker-compose.staging.yml run --rm migrate
```

Or locally:

```bash
scripts/run-migrations.sh
```

## Troubleshooting

| Issue | Solution |
|---|---|
| Backend won't start | Check `DATABASE_URL` and `INTERNAL_API_SECRET` are set |
| Health check fails | Verify PostgreSQL is healthy: `docker compose ps` |
| Connection refused | Backend binds to `127.0.0.1:8000` only |
| Migration fails | Check database connectivity and Alembic logs |

## What NOT to do

- Do not use real customer data or production phone numbers.
- Do not expose port 8000 to the public internet.
- Do not commit `.env.staging` with real secrets.
- Do not use this configuration for production deployment.
