#!/usr/bin/env bash
#
# Run Fonely PostgreSQL integration tests safely.
#
# Usage:
#   FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 scripts/test-postgres.sh
#   FONELY_TEST_DATABASE_URL=<url> FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 scripts/test-postgres.sh
#
# Only localhost databases are permitted. Remote hosts are denied.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_ROOT="${PROJECT_ROOT}/backend"
VENV_BIN="${BACKEND_ROOT}/.venv/bin"
ALEMBIC="${VENV_BIN}/alembic"
PYTEST="${VENV_BIN}/pytest"

DEFAULT_LOCAL_URL="postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test"

# ---------------------------------------------------------------------------
# Safety validation — uses Python's urllib.parse for correct URL handling
# ---------------------------------------------------------------------------

validate_database_url() {
    local url="$1"

    if [[ -z "${url}" ]]; then
        echo "ERROR: Database URL is empty." >&2
        exit 1
    fi

    if [[ "${url}" != postgresql+asyncpg://* ]]; then
        echo "ERROR: Database URL must use the postgresql+asyncpg:// scheme." >&2
        exit 1
    fi

    # Parse with Python — handles IPv6, percent-encoding, Unix sockets, query strings.
    # Returns "host database" on stdout. Never prints credentials.
    local parsed
    parsed=$(_FONELY_VALIDATE_URL="${url}" "${VENV_BIN}/python" -c '
import os, re, sys
from urllib.parse import urlparse
url = os.environ["_FONELY_VALIDATE_URL"]
p = urlparse(url.replace("+asyncpg", "", 1))
host = p.hostname or ""
db_name = (p.path.lstrip("/").split("/")[0] if p.path else "").lower()
if not db_name:
    print("Database name is empty.", file=sys.stderr); sys.exit(1)
if not re.fullmatch(r"fonely_test(_[a-z0-9_]+)?", db_name):
    print(f"Database name \x27{db_name}\x27 does not match: fonely_test or fonely_test_<suffix>.", file=sys.stderr)
    sys.exit(1)
if host not in ("localhost", "127.0.0.1", "::1"):
    print(f"Host \x27{host}\x27 is not localhost. Remote test databases are not permitted.", file=sys.stderr)
    sys.exit(1)
print(f"{host} {db_name}")
' 2>&1) || {
        echo "ERROR: URL validation failed." >&2
        echo "${parsed}" >&2
        exit 1
    }

    local host db_name
    host="${parsed%% *}"
    db_name="${parsed#* }"

    echo "Database URL validated: host=${host}, database=${db_name}"
}

# ---------------------------------------------------------------------------
# Resolve URL
# ---------------------------------------------------------------------------

if [[ -n "${FONELY_TEST_DATABASE_URL:-}" ]]; then
    TEST_URL="${FONELY_TEST_DATABASE_URL}"
    echo "Using explicitly provided FONELY_TEST_DATABASE_URL."
else
    TEST_URL="${DEFAULT_LOCAL_URL}"
    echo "Using default local test URL."
fi

validate_database_url "${TEST_URL}"

# ---------------------------------------------------------------------------
# Destructive opt-in
# ---------------------------------------------------------------------------

if [[ "${FONELY_ALLOW_DESTRUCTIVE_TEST_DB:-0}" != "1" ]]; then
    echo "ERROR: FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 is required." >&2
    echo "  This confirms you understand the test suite will downgrade/upgrade/truncate the target database." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Export for child processes — never print the password
# ---------------------------------------------------------------------------

export FONELY_TEST_DATABASE_URL="${TEST_URL}"
export FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1
export DATABASE_URL="${TEST_URL}"

# Build a plain-postgresql URL for the health-check driver
HEALTHCHECK_URL="${TEST_URL/postgresql+asyncpg/postgresql}"

# ---------------------------------------------------------------------------
# Wait for PostgreSQL to become healthy
# ---------------------------------------------------------------------------

MAX_RETRIES=30
RETRY_INTERVAL=2

echo "Waiting for PostgreSQL to become ready..."
for (( i=1; i<=MAX_RETRIES; i++ )); do
    if FONELY_HEALTHCHECK_URL="${HEALTHCHECK_URL}" "${VENV_BIN}/python" - <<'HEALTHCHECK_PY' 2>/dev/null; then
import asyncio, os, sys
async def check():
    try:
        import asyncpg
        conn = await asyncio.wait_for(
            asyncpg.connect(os.environ["FONELY_HEALTHCHECK_URL"]),
            timeout=3,
        )
        await conn.close()
    except Exception:
        sys.exit(1)
asyncio.run(check())
HEALTHCHECK_PY
        echo "PostgreSQL is ready (attempt ${i}/${MAX_RETRIES})."
        break
    fi

    if [[ ${i} -eq ${MAX_RETRIES} ]]; then
        echo "ERROR: PostgreSQL did not become ready after ${MAX_RETRIES} attempts." >&2
        exit 1
    fi

    sleep "${RETRY_INTERVAL}"
done

# ---------------------------------------------------------------------------
# Cleanup/restore trap
# ---------------------------------------------------------------------------

ORIGINAL_EXIT_CODE=0

cleanup() {
    local exit_code=$?
    if [[ ${exit_code} -ne 0 ]]; then
        ORIGINAL_EXIT_CODE=${exit_code}
    fi

    echo ""
    echo "--- Cleanup: restoring schema to head ---"
    cd "${BACKEND_ROOT}"
    "${ALEMBIC}" upgrade head 2>&1 || echo "WARNING: cleanup upgrade to head failed." >&2

    if [[ ${ORIGINAL_EXIT_CODE} -ne 0 ]]; then
        echo "FAILED with exit code ${ORIGINAL_EXIT_CODE}." >&2
        exit "${ORIGINAL_EXIT_CODE}"
    fi
}

trap cleanup EXIT

# ---------------------------------------------------------------------------
# Migration cycle + tests
# ---------------------------------------------------------------------------

cd "${BACKEND_ROOT}"

echo ""
echo "=== Step 1/6: Downgrade to base ==="
"${ALEMBIC}" downgrade base

echo ""
echo "=== Step 2/6: Upgrade to head ==="
"${ALEMBIC}" upgrade head

echo ""
echo "=== Step 3/6: Run PostgreSQL integration tests ==="
"${PYTEST}" -m postgres -q

echo ""
echo "=== Step 4/6: Downgrade to base ==="
"${ALEMBIC}" downgrade base

echo ""
echo "=== Step 5/6: Upgrade to head ==="
"${ALEMBIC}" upgrade head

echo ""
echo "=== Step 6/6: Alembic check ==="
"${ALEMBIC}" check

echo ""
echo "All PostgreSQL tests and migration checks passed."
