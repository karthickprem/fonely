# Database Deployment-Readiness Verifier

Non-destructive tool that checks whether a PostgreSQL database is reachable, at the expected Alembic revision, running a supported version, and capable of read-only transactions.

## What a pass proves

- The configured database is reachable within bounded time.
- PostgreSQL major version is in the supported set (14–17).
- The repository has exactly one Alembic migration head.
- The database revision exactly equals that head.
- A read-only transaction can be established and verified.

## What a pass does not prove

- Application correctness, provider connectivity, or channel readiness.
- That migrations ran successfully (use `alembic upgrade head` for that).
- Staging, pilot, or production readiness.
- Backup/restore capability.
- Data integrity or tenant isolation.

## Required environment variables

| Variable | Required | Description |
|---|---|---|
| `FONELY_READINESS_DATABASE_URL` | Yes | `postgresql+asyncpg://...` connection URL |
| `FONELY_READINESS_ENVIRONMENT` | Yes | Deployment label: `staging`, `github-ci`, etc. |
| `FONELY_READINESS_CONNECT_TIMEOUT_S` | No | Connection timeout in seconds (default: 10, max: 120) |
| `FONELY_READINESS_OVERALL_TIMEOUT_S` | No | Total execution timeout in seconds (default: 30, max: 300) |

`DATABASE_URL` is intentionally ignored. The verifier requires its own explicit URL to prevent accidental production use.

## Usage

### Local (after running migrations against a local test database)

```bash
FONELY_READINESS_DATABASE_URL="postgresql+asyncpg://fonely_test:fonely_test@localhost:5432/fonely_test" \
FONELY_READINESS_ENVIRONMENT="local-dev" \
python scripts/check-deployment-readiness.py
```

### CI

The Backend CI workflow runs the verifier automatically after `alembic upgrade head` against the disposable PostgreSQL 16 service. No additional setup is needed.

### Future staging pre-deploy / post-deploy

```bash
# Pre-deploy: verify current state before applying migrations
FONELY_READINESS_DATABASE_URL="$STAGING_DB_URL" \
FONELY_READINESS_ENVIRONMENT="staging" \
python scripts/check-deployment-readiness.py

# Apply migration through deployment process
alembic upgrade head

# Post-deploy: verify migration succeeded
FONELY_READINESS_DATABASE_URL="$STAGING_DB_URL" \
FONELY_READINESS_ENVIRONMENT="staging" \
python scripts/check-deployment-readiness.py
```

## JSON output

The verifier emits exactly one JSON document to stdout. All diagnostics go to stderr.

```json
{
  "schema_version": 1,
  "check_run_id": "<unique hex>",
  "checked_at": "2026-08-02T12:00:00Z",
  "environment": "staging",
  "overall_status": "passed",
  "total_duration_s": 1.234,
  "repository_head": "0004",
  "database_revision": "0004",
  "postgres_major": 16,
  "checks": [
    {"name": "repository_head", "status": "passed", ...},
    {"name": "connection", "status": "passed", ...},
    {"name": "postgres_version", "status": "passed", ...},
    {"name": "database_revision", "status": "passed", ...},
    {"name": "readonly_transaction", "status": "passed", ...}
  ]
}
```

## Exit codes

- `0` — all checks passed.
- `1` — one or more checks failed or timed out.

## Failure categories

| Code | Meaning | Operator response |
|---|---|---|
| `configuration_missing` | Required env var not set | Set the variable and retry |
| `configuration_invalid` | Malformed URL, label, or timeout | Fix the configuration |
| `connection_failed` | Database unreachable | Check network, host, port, credentials |
| `connection_timeout` | Connection took too long | Check network path and database load |
| `unsupported_postgres_version` | Major version outside 14–17 | Use a supported PostgreSQL version |
| `repository_head_missing` | No Alembic revisions found | Check repository checkout |
| `repository_heads_multiple` | Branched migration history | Resolve to a single Alembic head |
| `alembic_version_missing` | No `alembic_version` table | Run initial migration |
| `database_revision_invalid` | Empty or malformed revision | Investigate database state |
| `database_revision_stale` | Database behind repository head | Apply pending migrations |
| `readonly_check_failed` | Cannot verify read-only mode | Investigate database permissions |
| `overall_timeout` | Total execution exceeded limit | Increase timeout or investigate |
| `internal_error` | Unexpected error | Report for investigation |

## Credential redaction

The verifier never prints database URLs, usernames, passwords, or raw exception messages containing connection strings. All output is sanitized before emission.

## Non-destructive guarantee

The verifier performs no writes. It issues only:

- `SELECT 1`
- `SHOW server_version`
- `SELECT EXISTS (... information_schema.tables ...)`
- `SELECT version_num FROM alembic_version`
- `SET TRANSACTION READ ONLY`
- `SHOW transaction_read_only`

It does not run `alembic upgrade`, `alembic check`, or any DDL/DML.

## Distinction from other tools

| Tool | Purpose | Destructive? |
|---|---|---|
| `scripts/check-deployment-readiness.py` | Verify database is ready for application startup | No |
| `scripts/test-postgres.sh` | Run PostgreSQL integration tests with migration cycles | Yes (test databases only) |
| `alembic upgrade head` | Apply pending migrations | Yes (schema changes) |
| `alembic check` | Verify ORM matches current head | No (but requires migration env) |

## Future deployment sequence

1. Verify backup/restore readiness (separate process).
2. Run pre-deploy readiness check.
3. Apply approved migration through deployment process.
4. Run post-deploy readiness check.
5. Start application traffic only after all required gates pass.
6. Monitor.
7. Follow rollback or forward-fix policy on failure.
