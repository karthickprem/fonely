#!/usr/bin/env bash
# Read-only pre-push audit for Fonely candidate files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MAX_BYTES=$((10 * 1024 * 1024))
TMPDIR_WORK="$(mktemp -d)"
CANDIDATES_FILE="${TMPDIR_WORK}/candidates.nul"

cleanup() {
    rm -rf "${TMPDIR_WORK}"
}
trap cleanup EXIT

build_candidate_list() {
    if git -C "${PROJECT_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "${PROJECT_ROOT}" ls-files --cached --others --exclude-standard -z \
            > "${CANDIDATES_FILE}"
        return
    fi

    git init -q "${TMPDIR_WORK}/audit-repo"
    git \
        --git-dir="${TMPDIR_WORK}/audit-repo/.git" \
        --work-tree="${PROJECT_ROOT}" \
        -c core.excludesFile=/dev/null \
        ls-files --cached --others --exclude-standard -z \
        > "${CANDIDATES_FILE}"
}

build_candidate_list

FINDINGS=0
CANDIDATE_COUNT=0

report() {
    local category="$1"
    local path="$2"
    printf 'FINDING [%s] %s\n' "${category}" "${path}" >&2
    FINDINGS=$((FINDINGS + 1))
}

is_forbidden_path() {
    local path="$1"
    case "${path}" in
        .env|*/.env|.env.*|*/.env.*)
            [[ "${path}" == ".env.example" || "${path}" == */.env.example ]] && return 1
            return 0
            ;;
        */.venv/*|.venv/*|*/__pycache__/*|__pycache__/*|*/.pytest_cache/*|.pytest_cache/*|*/.mypy_cache/*|.mypy_cache/*|*/.ruff_cache/*|.ruff_cache/*)
            return 0
            ;;
        *.pyc|*.pyo|*.db|*.sqlite|*.sqlite3|*.log|*.pem|*.key|id_ed25519*|*/id_ed25519*)
            return 0
            ;;
        */node_modules/*|node_modules/*|*/dist/*|dist/*|*/test_output/*|test_output/*|*/voice_samples/*|voice_samples/*|*/evals/results/*|evals/results/*|*/.ssh/*|.ssh/*)
            return 0
            ;;
        *.wav|*.mp3|*.raw|*.pcm)
            return 0
            ;;
    esac
    return 1
}

while IFS= read -r -d '' relative_path; do
    CANDIDATE_COUNT=$((CANDIDATE_COUNT + 1))
    absolute_path="${PROJECT_ROOT}/${relative_path}"

    if is_forbidden_path "${relative_path}"; then
        report "forbidden-path" "${relative_path}"
        continue
    fi

    if [[ ! -f "${absolute_path}" ]]; then
        continue
    fi

    size=$(stat -c '%s' "${absolute_path}")
    if (( size > MAX_BYTES )); then
        report "file-over-10MiB" "${relative_path}"
    fi
done < "${CANDIDATES_FILE}"

export FONELY_AUDIT_ROOT="${PROJECT_ROOT}"
export FONELY_AUDIT_CANDIDATES="${CANDIDATES_FILE}"

secret_findings=$("${PROJECT_ROOT}/backend/.venv/bin/python" <<'PY'
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

root = Path(os.environ["FONELY_AUDIT_ROOT"])
candidates_path = Path(os.environ["FONELY_AUDIT_CANDIDATES"])
raw_paths = candidates_path.read_bytes().split(b"\0")
paths = [Path(value.decode("utf-8", errors="strict")) for value in raw_paths if value]

patterns = (
    ("sarvam-api-key", re.compile(r"\bsk_[A-Za-z0-9_-]{20,}\b")),
    ("generic-sk-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}")),
    ("private-key-header", re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----")),
    (
        "exotel-credential",
        re.compile(
            r"(?i)\bEXOTEL_[A-Z_]*(?:KEY|TOKEN|SECRET)[ \t]*=[ \t]*['\"]?([^\s'\"]+)"
        ),
    ),
    (
        "amd-gateway-key",
        re.compile(
            r"(?i)\b(?:Ocp-Apim-Subscription-Key|AMD_LLM_API_KEY)[ \t]*=[ \t]*['\"]?([^\s'\"]+)"
        ),
    ),
    (
        "credentialed-database-url",
        re.compile(
            r"(?i)\bpostgres(?:ql)?(?:\+asyncpg)?://[^\s:/\"']+:[^\s@\"']+@[^\s\"']+"
        ),
    ),
)

placeholders = (
    "your_",
    "example",
    "placeholder",
    "xxxx",
    "dummy",
    "changeme",
    "test-only",
    "user:password",
    "user:pass",
    "username:password",
    "fonely_test_user:secret",
    "app_user:secret",
)


def is_safe_test_database(match: str) -> bool:
    try:
        parsed = urlparse(match.replace("+asyncpg", ""))
    except ValueError:
        return False
    database = parsed.path.rsplit("/", 1)[-1].lower()
    username = (parsed.username or "").lower()
    host = (parsed.hostname or "").lower()
    return (
        database.startswith("fonely_test")
        and "test" in username
        and host in {"localhost", "127.0.0.1", "::1"}
    )

findings: set[tuple[str, str]] = set()
for relative_path in paths:
    absolute_path = root / relative_path
    if not absolute_path.is_file():
        continue
    data = absolute_path.read_bytes()
    if b"\0" in data:
        continue
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        continue
    for category, pattern in patterns:
        for match in pattern.finditer(text):
            matched = match.group(0)
            lowered = matched.lower()
            if any(marker in lowered for marker in placeholders):
                continue
            if category == "credentialed-database-url" and is_safe_test_database(matched):
                continue
            findings.add((category, relative_path.as_posix()))

for category, path in sorted(findings):
    print(f"{category}\t{path}")
PY
)

if [[ -n "${secret_findings}" ]]; then
    while IFS=$'\t' read -r category path; do
        [[ -z "${category}" ]] && continue
        report "${category}" "${path}"
    done <<< "${secret_findings}"
fi

printf 'Audited %d candidate files.\n' "${CANDIDATE_COUNT}"
if (( FINDINGS > 0 )); then
    printf 'Pre-push audit FAILED with %d finding(s).\n' "${FINDINGS}" >&2
    exit 1
fi

printf 'Pre-push audit PASSED. No blocked artifacts or credential patterns found.\n'
