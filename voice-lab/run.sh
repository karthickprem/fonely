#!/usr/bin/env bash
set -euo pipefail

LAB_DIR=$(cd "$(dirname "$0")" && pwd)
CLIENT_DIR="$LAB_DIR/client"
PYTHON=${VOICE_LAB_PYTHON:-/scratch/karthick/fonely/backend/.venv/bin/python}
BUN=${VOICE_LAB_BUN:-/home/karthick/.bun/bin/bun}

if [[ ! -x "$PYTHON" ]]; then
  echo "Voice lab Python not found: $PYTHON" >&2
  exit 1
fi
if [[ ! -x "$BUN" ]]; then
  echo "Bun not found: $BUN" >&2
  exit 1
fi

if [[ ! -d "$CLIENT_DIR/node_modules" ]]; then
  (cd "$CLIENT_DIR" && "$BUN" install --frozen-lockfile)
fi
(cd "$CLIENT_DIR" && "$BUN" run build)

export PYTHONUNBUFFERED=1
export PYTHONSAFEPATH=1
export NLTK_DATA=${NLTK_DATA:-/scratch/karthick/nltk_data}
export PIPECAT_SMART_TURN_LOG_DATA=false

cd "$LAB_DIR"
exec "$PYTHON" server_webrtc.py --host 0.0.0.0 --port "${VOICE_LAB_PORT:-3000}" -t webrtc
