#!/usr/bin/env bash
# Controlled launch wrapper for the two-tab voice-lab server on :3000.
#
# Provides the DB/provider variable NAMES the server needs from protected sources
# WITHOUT eval/sourcing untrusted files, without printing values, and without
# putting secrets on the command line:
#   * SARVAM_API_KEY / CARTESIA_API_KEY / CARTESIA_VOICE_ID — selectively
#     extracted (non-executing sed) from the protected Fonely .env.
#   * ANTHROPIC_BASE_URL / ANTHROPIC_CUSTOM_HEADERS / LLM_GATEWAY_KEY / NLTK_DATA —
#     inherited from the launching shell (already exported there for the Fonely
#     booking LLM); this wrapper does not fabricate them.
#   * DATABASE_URL / INTERNAL_API_SECRET / WHATSAPP_BUSINESS_MAPPINGS — the app's
#     production_wiring sets these via os.environ.setdefault at import, so they
#     are intentionally NOT provided here (the demo DB fonely_dev4 default).
#
# Fails closed on any missing required NAME. Names-only readiness log.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRET_SRC="/scratch/karthick/fonely/.env"
PY="${VOICE_LAB_PYTHON:-/scratch/karthick/fonely/backend/.venv/bin/python}"
HOST="${VOICE_LAB_HOST:-0.0.0.0}"
PORT="${VOICE_LAB_PORT:-3000}"

[[ -f "$SECRET_SRC" ]] || { echo "FATAL: protected .env missing" >&2; exit 3; }
_perm="$(stat -c '%a' "$SECRET_SRC" 2>/dev/null || echo 000)"
if [[ "${_perm: -1}" =~ [2367] ]]; then
  echo "FATAL: protected .env is world-writable (perms $_perm) — refusing" >&2
  exit 5
fi

_extract_name() {
  # NON-EXECUTING: sed only prints the literal RHS; never evals the file.
  local name="$1" matches
  matches="$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?${name}[[:space:]]*=[[:space:]]*(.*)$/\2/p" "$SECRET_SRC")"
  if (( $(printf '%s' "$matches" | grep -c . ) > 1 )); then
    echo "FATAL: $name defined more than once in .env — ambiguous" >&2
    exit 6
  fi
  printf '%s' "$matches" | sed -E 's/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
}

export SARVAM_API_KEY="$(_extract_name SARVAM_API_KEY)"
export CARTESIA_API_KEY="$(_extract_name CARTESIA_API_KEY)"
export CARTESIA_VOICE_ID="$(_extract_name CARTESIA_VOICE_ID)"

# Redirect nltk/caches off the full home quota (nltk resources are staged on NFS).
export NLTK_DATA="${NLTK_DATA:-/everest/apex_pvs_nobkup/karthick/tmp/hf-s2s-20260815/.nltk}"

missing=()
for name in SARVAM_API_KEY CARTESIA_API_KEY CARTESIA_VOICE_ID \
            ANTHROPIC_BASE_URL LLM_GATEWAY_KEY; do
  [[ -n "${!name:-}" ]] || missing+=("$name")
done
if (( ${#missing[@]} > 0 )); then
  echo "FATAL: required env names unset: ${missing[*]}" >&2
  echo "  (SARVAM/CARTESIA from .env; ANTHROPIC_BASE_URL + LLM_GATEWAY_KEY from the launching shell)" >&2
  exit 4
fi

echo "voice-lab :$PORT launch readiness (names only):"
for name in SARVAM_API_KEY CARTESIA_API_KEY CARTESIA_VOICE_ID \
            ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS LLM_GATEWAY_KEY NLTK_DATA; do
  [[ -n "${!name:-}" ]] && echo "  ok: \$$name is set" || echo "  --: \$$name unset (app default)"
done

if [[ "${1:-}" == "--check" ]]; then
  echo "  --check: names validated, not launching."
  exit 0
fi

export PYTHONUNBUFFERED=1
export PYTHONSAFEPATH=1
export PIPECAT_SMART_TURN_LOG_DATA=false
cd "$LAB_DIR"
exec "$PY" server_webrtc.py --host "$HOST" --port "$PORT" -t webrtc
