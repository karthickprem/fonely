#!/usr/bin/env bash
# Run staging locally with a public TLS URL via cloudflared tunnel.
#
# Requires:
#   - /tmp/pg16 (from dev-postgres.sh)
#   - /tmp/cloudflared binary
#   - backend/.venv with dependencies installed
#
# Usage:
#   ./scripts/dev-staging.sh start   # start PG + app + tunnel
#   ./scripts/dev-staging.sh stop    # stop everything
#   ./scripts/dev-staging.sh url     # print the tunnel URL
#
# SECURITY:
#   - /webhooks/exotel/* is BLOCKED at the tunnel edge
#   - Seed data only — no real credentials or customer data
#   - Report the URL to CEO before sharing with any external service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
PG_PREFIX="/tmp/pg16"
CLOUDFLARED="/tmp/cloudflared"
PID_DIR="/tmp/fonely-staging"
APP_PORT="8000"
PG_PORT="55432"
PG_USER="fonely_test"
PG_DB="fonely_staging"

export PATH="${PG_PREFIX}/bin:${PATH}"
export LD_LIBRARY_PATH="${PG_PREFIX}/lib:${LD_LIBRARY_PATH:-}"

mkdir -p "$PID_DIR"

start_pg() {
  if pg_isready -h localhost -p "$PG_PORT" -U "$PG_USER" -q 2>/dev/null; then
    echo "PostgreSQL already running"
  else
    "${SCRIPT_DIR}/dev-postgres.sh" start
  fi
  createdb -h localhost -p "$PG_PORT" -U "$PG_USER" "$PG_DB" 2>/dev/null || true
  DATABASE_URL="postgresql+asyncpg://${PG_USER}@localhost:${PG_PORT}/${PG_DB}" \
    "${BACKEND_DIR}/.venv/bin/alembic" -c "${BACKEND_DIR}/alembic.ini" upgrade head
}

start_app() {
  if [ -f "$PID_DIR/app.pid" ] && kill -0 "$(cat "$PID_DIR/app.pid")" 2>/dev/null; then
    echo "App already running"
    return
  fi

  KNOWN_PLACEHOLDERS="staging-only-secret staging-secret test password secret changeme"
  if [ -z "${INTERNAL_API_SECRET:-}" ]; then
    INTERNAL_API_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    echo "Generated INTERNAL_API_SECRET (not logged)."
    echo "$INTERNAL_API_SECRET" > "$PID_DIR/api-secret.txt"
    chmod 600 "$PID_DIR/api-secret.txt"
    echo "Secret stored at $PID_DIR/api-secret.txt (read it to make internal API calls)"
  else
    for placeholder in $KNOWN_PLACEHOLDERS; do
      if [ "$INTERNAL_API_SECRET" = "$placeholder" ]; then
        echo "ERROR: INTERNAL_API_SECRET matches known placeholder '$placeholder'. Use a strong random value."
        exit 1
      fi
    done
  fi

  echo "Starting uvicorn on port ${APP_PORT}..."
  DATABASE_URL="postgresql+asyncpg://${PG_USER}@localhost:${PG_PORT}/${PG_DB}" \
  INTERNAL_API_SECRET="${INTERNAL_API_SECRET}" \
  OPENAPI_URL="" \
  WHATSAPP_VERIFY_TOKEN="${WHATSAPP_VERIFY_TOKEN:-staging-verify-token}" \
  WHATSAPP_ACCESS_TOKEN="${WHATSAPP_ACCESS_TOKEN:-}" \
  WHATSAPP_PHONE_NUMBER_ID="${WHATSAPP_PHONE_NUMBER_ID:-}" \
  WHATSAPP_BUSINESS_MAPPINGS="${WHATSAPP_BUSINESS_MAPPINGS:-{}}" \
  WHATSAPP_APP_SECRET="${WHATSAPP_APP_SECRET:-}" \
  LOG_FORMAT=json \
  LOG_LEVEL=INFO \
    nohup "${BACKEND_DIR}/.venv/bin/uvicorn" \
      fonely.app:create_app --factory \
      --host 127.0.0.1 --port "$APP_PORT" \
      > "$PID_DIR/app.log" 2>&1 &
  echo $! > "$PID_DIR/app.pid"
  sleep 2
  if ! kill -0 "$(cat "$PID_DIR/app.pid")" 2>/dev/null; then
    echo "ERROR: App failed to start. Check $PID_DIR/app.log"
    exit 1
  fi
  echo "App running on 127.0.0.1:${APP_PORT}"
}

start_tunnel() {
  if [ -f "$PID_DIR/tunnel.pid" ] && kill -0 "$(cat "$PID_DIR/tunnel.pid")" 2>/dev/null; then
    echo "Tunnel already running"
    return
  fi
  echo "Starting cloudflared tunnel..."
  nohup "$CLOUDFLARED" tunnel --url "http://127.0.0.1:${APP_PORT}" \
    > "$PID_DIR/tunnel.log" 2>&1 &
  echo $! > "$PID_DIR/tunnel.pid"
  sleep 5
  URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$PID_DIR/tunnel.log" 2>/dev/null | head -1)
  if [ -n "$URL" ]; then
    echo "$URL" > "$PID_DIR/tunnel.url"
    echo ""
    echo "=========================================="
    echo "  STAGING URL: ${URL}"
    echo "=========================================="
    echo ""
    echo "BLOCKED by app config (EXOTEL_WEBHOOK_SECRET unset):"
    echo "  /webhooks/exotel/* (route not mounted)"
    echo ""
    echo "Available paths:"
    echo "  ${URL}/health/live"
    echo "  ${URL}/health/ready"
    echo "  ${URL}/webhooks/whatsapp (requires HMAC)"
    echo ""
    echo "Report this URL to CEO before sharing externally."
  else
    echo "WARNING: Could not extract tunnel URL. Check $PID_DIR/tunnel.log"
  fi
}

stop_all() {
  for svc in tunnel app; do
    if [ -f "$PID_DIR/${svc}.pid" ]; then
      pid=$(cat "$PID_DIR/${svc}.pid")
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        echo "Stopped ${svc} (pid ${pid})"
      fi
      rm -f "$PID_DIR/${svc}.pid"
    fi
  done
}

case "${1:-help}" in
  start)
    start_pg
    start_app
    start_tunnel
    ;;
  stop)
    stop_all
    ;;
  url)
    if [ -f "$PID_DIR/tunnel.url" ]; then
      cat "$PID_DIR/tunnel.url"
    else
      echo "No tunnel URL found. Run: $0 start"
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|url}"
    exit 1
    ;;
esac
