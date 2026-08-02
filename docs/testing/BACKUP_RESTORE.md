# PostgreSQL Backup-and-Restore Verification

Provider-neutral logical backup-and-restore contract for disposable test databases. Proves that an operator can back up a migrated Fonely database, restore it into a separate empty database, and verify schema, data, and revision integrity.

## What a pass proves

- pg_dump custom-format backup completes from a migrated disposable database.
- pg_restore restores schema, data, constraints, functions, and triggers into a separate disposable database.
- Alembic revision in the restored database exactly matches the source.
- Content-sensitive evidence digest proves exact synthetic row values, tenant relationships, and schema definitions survive — not merely aggregate counts.
- A same-count content mutation or tenant relationship swap would change the digest and fail verification.
- Source evidence digest is unchanged after backup/restore.
- Temporary backup files are cleaned up.

The evidence digest covers the bounded synthetic verification dataset (businesses, users, services, resources, and schema objects). It does not prove unchanged state for arbitrary database contents outside that contract.

## What a pass does not prove

- Production backup storage, retention, or encryption.
- RPO/RTO compliance.
- Point-in-time recovery.
- Live restore drills against production data.
- Application-level health after restore.
- Provider-specific backup services (e.g. RDS snapshots).

## Required environment variables

| Variable | Required | Description |
|---|---|---|
| `FONELY_BACKUP_SOURCE_URL` | Yes | `postgresql://` source database (migrated, seeded) |
| `FONELY_BACKUP_RESTORE_URL` | Yes | `postgresql://` restore target (empty, disposable) |
| `FONELY_BACKUP_ENVIRONMENT` | Yes | Deployment label: `github-ci`, etc. |
| `FONELY_BACKUP_TIMEOUT_S` | No | Overall timeout in seconds (default: 120, max: 600) |

Both URLs must point to disposable test databases (`fonely_test` or `fonely_test_<suffix>`) on localhost with a test-role user.

## Safety guards

- Database names must match `fonely_test` or `fonely_test_<suffix>`.
- Users must contain `test`.
- Hosts must be `localhost`, `127.0.0.1`, or `::1`.
- Source and target must be different databases.
- `DATABASE_URL` is never used.
- No credentials appear in output or logs.

## CI usage

The Backend CI workflow creates a second disposable database, seeds synthetic data, runs the full backup/restore/verify cycle, and drops the restore database afterward.

## JSON output

One stable JSON document on stdout:

```json
{
  "schema_version": 1,
  "run_id": "<hex>",
  "checked_at": "2026-08-02T12:00:00Z",
  "environment": "github-ci",
  "overall_status": "passed",
  "postgres_major": 16,
  "source_revision": "0004",
  "restored_revision": "0004",
  "checks": [
    {"name": "source_version", ...},
    {"name": "source_revision", ...},
    {"name": "backup", ...},
    {"name": "restore", ...},
    {"name": "restored_revision", ...},
    {"name": "schema_objects", ...},
    {"name": "data_integrity", ...},
    {"name": "source_unchanged", ...},
    {"name": "cleanup", ...}
  ]
}
```

## Exit codes

- `0` — all checks passed.
- `1` — one or more checks failed.

## Distinction from other tools

| Tool | Purpose |
|---|---|
| `verify-backup-restore.py` | Prove logical backup/restore works for disposable test databases |
| `check-deployment-readiness.py` | Verify a database is ready for application startup |
| `test-postgres.sh` | Run PostgreSQL integration tests with migration cycles |
