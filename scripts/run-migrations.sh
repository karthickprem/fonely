#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../backend" && pwd)"
cd "$BACKEND_DIR"

echo "Running Alembic upgrade to head..."
.venv/bin/alembic upgrade head

CURRENT=$(.venv/bin/alembic current 2>/dev/null | head -1 | awk '{print $1}')
echo "Current revision: ${CURRENT}"

if [ -z "$CURRENT" ]; then
    echo "ERROR: No current Alembic revision after upgrade."
    exit 1
fi

echo "Migration complete."
