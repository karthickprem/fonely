#!/usr/bin/env bash
# Local PostgreSQL for developer worktrees — no Docker, no root.
#
# Usage:
#   ./scripts/dev-postgres.sh start    # start PG on port 55432
#   ./scripts/dev-postgres.sh stop     # stop PG
#   ./scripts/dev-postgres.sh status   # check if running
#   ./scripts/dev-postgres.sh test     # run PG integration tests
#   ./scripts/dev-postgres.sh reset    # drop and recreate test database
#
# Requires: /tmp/pg16 (built from source, see docs/testing/LOCAL_PG_SETUP.md)

set -euo pipefail

PG_PREFIX="/tmp/pg16"
PG_DATA="/tmp/fonely_pgdata"
PG_PORT="55432"
PG_USER="fonely_test"
PG_DB="fonely_test"
PG_LOG="/tmp/fonely_pg.log"
PG_SOCKET="/tmp"

export PATH="${PG_PREFIX}/bin:${PATH}"
export LD_LIBRARY_PATH="${PG_PREFIX}/lib:${LD_LIBRARY_PATH:-}"
if [ "${TZ:-}" != "UTC" ]; then
  echo "NOTE: Setting TZ=UTC for CI parity (was ${TZ:-system default})."
fi
export TZ=UTC

CONNURL="postgresql+asyncpg://${PG_USER}@localhost:${PG_PORT}/${PG_DB}"

case "${1:-help}" in
  start)
    if pg_isready -h localhost -p "$PG_PORT" -U "$PG_USER" -q 2>/dev/null; then
      echo "PostgreSQL already running on port ${PG_PORT}"
      exit 0
    fi
    if [ ! -d "$PG_DATA" ]; then
      echo "Initializing database cluster..."
      initdb -D "$PG_DATA" --no-locale -E UTF8 -U "$PG_USER" >/dev/null
    fi
    echo "Starting PostgreSQL on port ${PG_PORT}..."
    pg_ctl -D "$PG_DATA" -l "$PG_LOG" -o "-p ${PG_PORT} -k ${PG_SOCKET}" start
    sleep 1
    if ! createdb -h localhost -p "$PG_PORT" -U "$PG_USER" "$PG_DB" 2>/dev/null; then
      true
    fi
    echo "Ready. Connection URL:"
    echo "  ${CONNURL}"
    echo ""
    echo "To run PG tests:"
    echo "  FONELY_TEST_DATABASE_URL='${CONNURL}' DATABASE_URL='${CONNURL}' FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 .venv/bin/pytest -m postgres -q"
    ;;
  stop)
    if pg_isready -h localhost -p "$PG_PORT" -U "$PG_USER" -q 2>/dev/null; then
      pg_ctl -D "$PG_DATA" stop
      echo "PostgreSQL stopped."
    else
      echo "PostgreSQL not running."
    fi
    ;;
  status)
    if pg_isready -h localhost -p "$PG_PORT" -U "$PG_USER" 2>/dev/null; then
      echo "PostgreSQL running on port ${PG_PORT}"
      psql -h localhost -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -c "SELECT version();" 2>/dev/null || true
    else
      echo "PostgreSQL not running."
      exit 1
    fi
    ;;
  reset)
    echo "Dropping and recreating ${PG_DB}..."
    dropdb -h localhost -p "$PG_PORT" -U "$PG_USER" "$PG_DB" 2>/dev/null || true
    createdb -h localhost -p "$PG_PORT" -U "$PG_USER" "$PG_DB"
    echo "Database reset."
    ;;
  test)
    if ! pg_isready -h localhost -p "$PG_PORT" -U "$PG_USER" -q 2>/dev/null; then
      echo "PostgreSQL not running. Run: $0 start"
      exit 1
    fi
    echo "Resetting database..."
    dropdb -h localhost -p "$PG_PORT" -U "$PG_USER" "$PG_DB" 2>/dev/null || true
    createdb -h localhost -p "$PG_PORT" -U "$PG_USER" "$PG_DB"
    echo "Running PG integration tests..."
    cd "$(dirname "$0")/../backend"
    FONELY_TEST_DATABASE_URL="${CONNURL}" \
    DATABASE_URL="${CONNURL}" \
    FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 \
    .venv/bin/pytest -m postgres -q -p no:timeout "$@"
    ;;
  *)
    echo "Usage: $0 {start|stop|status|test|reset}"
    exit 1
    ;;
esac
