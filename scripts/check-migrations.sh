#!/usr/bin/env bash
# Fail-closed wrapper for the offline migration policy checker.

set -euo pipefail

fail() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

REALPATH=/usr/bin/realpath
FIND=/usr/bin/find
MKTEMP=/usr/bin/mktemp
TIMEOUT=/usr/bin/timeout
RM=/usr/bin/rm
ENV=/usr/bin/env
GREP=/usr/bin/grep
DIRNAME=/usr/bin/dirname
for utility in "${REALPATH}" "${FIND}" "${MKTEMP}" "${TIMEOUT}" "${RM}" "${ENV}" "${GREP}" "${DIRNAME}"; do
    [[ -x "${utility}" ]] || fail "required system utility validation failed"
done

SCRIPT_DIR=$(cd "$("${DIRNAME}" "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd -P)
resolve_from() {
    local base=$1
    local value=$2
    if [[ "${value}" = /* ]]; then
        "${REALPATH}" -e -- "${value}"
    else
        "${REALPATH}" -e -- "${base}/${value}"
    fi
}

BACKEND_ROOT=$(resolve_from "${PROJECT_ROOT}" "${BACKEND_ROOT:-backend}") \
    || fail "backend path validation failed"
VERSIONS_DIR=$(resolve_from "${BACKEND_ROOT}" "migrations/versions") \
    || fail "versions path validation failed"
ALEMBIC_CONFIG=$(resolve_from "${BACKEND_ROOT}" "alembic.ini") \
    || fail "Alembic configuration validation failed"
POLICY_HELPER=$(resolve_from "${PROJECT_ROOT}" "scripts/migration_policy.py") \
    || fail "policy helper validation failed"
PYTHON_BIN=$(resolve_from "${BACKEND_ROOT}" ".venv/bin/python") \
    || fail "Python executable validation failed"
VENV_ROOT=$(resolve_from "${BACKEND_ROOT}" ".venv") \
    || fail "virtual environment validation failed"
VENV_SITE_PACKAGES=$("${FIND}" "${VENV_ROOT}/lib" -mindepth 2 -maxdepth 2 -type d -name site-packages -print -quit)
[[ -n "${VENV_SITE_PACKAGES}" ]] || fail "virtual environment site-packages validation failed"
ALEMBIC_BIN=$(resolve_from "${BACKEND_ROOT}" ".venv/bin/alembic") \
    || fail "Alembic executable validation failed"

[[ -d "${BACKEND_ROOT}" ]] || fail "backend path is not a directory"
[[ -d "${VERSIONS_DIR}" ]] || fail "versions path is not a directory"
[[ -f "${ALEMBIC_CONFIG}" ]] || fail "Alembic configuration is not a regular file"
[[ -f "${POLICY_HELPER}" ]] || fail "policy helper is not a regular file"
[[ -x "${PYTHON_BIN}" ]] || fail "Python executable is not executable"
[[ -x "${ALEMBIC_BIN}" ]] || fail "Alembic executable is not executable"

RENDER_TIMEOUT_SECONDS=${MIGRATION_RENDER_TIMEOUT:-30}
[[ ${#RENDER_TIMEOUT_SECONDS} -le 3 ]] \
    || fail "render timeout must be a bounded positive integer"
[[ "${RENDER_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ ]] \
    || fail "render timeout must be a positive integer"
[[ ${RENDER_TIMEOUT_SECONDS} -le 300 ]] || fail "render timeout exceeds hard cap"
REVISION_CANDIDATES=$("${FIND}" "${VERSIONS_DIR}" -type f -name '*.py' ! -name '__init__.py' -print | /usr/bin/wc -l)
[[ "${REVISION_CANDIDATES}" =~ ^[1-9][0-9]*$ ]] || fail "revision count validation failed"
[[ ${REVISION_CANDIDATES} -le 500 ]] || fail "revision count exceeds hard cap"
AGGREGATE_TIMEOUT_SECONDS=$((30 + RENDER_TIMEOUT_SECONDS * (2 + 2 * REVISION_CANDIDATES)))
[[ ${AGGREGATE_TIMEOUT_SECONDS} -le 3600 ]] \
    || fail "aggregate timeout exceeds hard cap"

TMPDIR_WORK=$("${MKTEMP}" -d)
cleanup() { "${RM}" -rf "${TMPDIR_WORK}"; }
trap cleanup EXIT
RESULT_FILE="${TMPDIR_WORK}/result.json"
DIAGNOSTIC_FILE="${TMPDIR_WORK}/helper.stderr"

set +e
"${ENV}" -i PATH="/usr/bin:/bin" HOME="${HOME:-/tmp}" LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    VIRTUAL_ENV="${VENV_ROOT}" PYTHONPATH="${VENV_SITE_PACKAGES}" \
    DATABASE_URL="postgresql+asyncpg://localhost:55432/fonely_test" \
    "${TIMEOUT}" --signal=TERM --kill-after=5 "${AGGREGATE_TIMEOUT_SECONDS}" \
    "${PYTHON_BIN}" "${POLICY_HELPER}" check \
    --backend-root "${BACKEND_ROOT}" \
    --versions-dir "${VERSIONS_DIR}" \
    --alembic-config "${ALEMBIC_CONFIG}" \
    --alembic "${ALEMBIC_BIN}" \
    --render-timeout "${RENDER_TIMEOUT_SECONDS}" \
    >"${RESULT_FILE}" 2>"${DIAGNOSTIC_FILE}"
HELPER_STATUS=$?
set -e

[[ ${HELPER_STATUS} -eq 0 ]] || fail "migration policy helper process failed"
[[ ! -s "${DIAGNOSTIC_FILE}" ]] || fail "migration policy helper emitted unexpected diagnostics"
[[ -s "${RESULT_FILE}" ]] || fail "migration policy helper returned empty output"

VALIDATED_FILE="${TMPDIR_WORK}/validated.txt"
set +e
"${PYTHON_BIN}" - "${RESULT_FILE}" "${REVISION_CANDIDATES}" >"${VALIDATED_FILE}" <<'PY'
import json
import re
import sys
from pathlib import Path

SAFE_TEXT = re.compile(r"^[ -~]{1,200}$")
SAFE_HEAD = re.compile(r"^[A-Za-z0-9_]{1,32}$")

try:
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    expected_revisions = int(sys.argv[2])
    decoder = json.JSONDecoder()
    data, end = decoder.raw_decode(text)
    if text[end:].strip():
        raise ValueError
    required = {
        "protocol_version",
        "ok",
        "findings",
        "errors",
        "revision_count",
        "head",
        "evidence",
        "ddl_counts",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError
    if data["protocol_version"] != 1 or data["ok"] is not True:
        raise ValueError
    if not isinstance(data["findings"], list) or not all(
        isinstance(item, str)
        and SAFE_TEXT.fullmatch(item)
        and not item.startswith(("INFO:", "ERROR:"))
        for item in data["findings"]
    ):
        raise ValueError
    if data["errors"] != []:
        raise ValueError
    if (
        type(data["revision_count"]) is not int
        or data["revision_count"] != expected_revisions
        or not 1 <= data["revision_count"] <= 500
    ):
        raise ValueError
    if not isinstance(data["head"], str) or not SAFE_HEAD.fullmatch(data["head"]):
        raise ValueError
    evidence = data["evidence"]
    if not isinstance(evidence, dict) or evidence != {
        "cumulative_upgrade_rendered": True,
        "cumulative_downgrade_rendered": True,
    }:
        raise ValueError
    counts = data["ddl_counts"]
    expected_counts = {
        "create_table",
        "drop_table",
        "add_column",
        "drop_column",
        "check_constraint",
        "create_index",
        "drop_index",
    }
    if not isinstance(counts, dict) or set(counts) != expected_counts:
        raise ValueError
    if not all(type(value) is int and value >= 0 for value in counts.values()):
        raise ValueError
except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
    sys.exit(1)

print(f"revision_count={data['revision_count']}")
print(f"head={data['head']}")
for finding in data["findings"]:
    print(f"finding={finding}")
for key in sorted(data["ddl_counts"]):
    print(f"count_{key}={data['ddl_counts'][key]}")
PY
VALIDATION_STATUS=$?
set -e
[[ ${VALIDATION_STATUS} -eq 0 ]] || fail "migration policy helper protocol validation failed"
[[ -s "${VALIDATED_FILE}" ]] || fail "migration policy helper protocol validation failed"
[[ $("${GREP}" -c '^revision_count=' "${VALIDATED_FILE}") -eq 1 ]] \
    || fail "migration policy helper protocol validation failed"
[[ $("${GREP}" -c '^head=' "${VALIDATED_FILE}") -eq 1 ]] \
    || fail "migration policy helper protocol validation failed"
[[ $("${GREP}" -c '^count_' "${VALIDATED_FILE}") -eq 7 ]] \
    || fail "migration policy helper protocol validation failed"

printf 'INFO: Migration policy PASSED.\n'
while IFS= read -r line; do
    case "${line}" in
        revision_count=*) printf 'INFO: Revisions accounted: %s\n' "${line#revision_count=}" ;;
        head=*) printf 'INFO: Effective head: %s\n' "${line#head=}" ;;
        finding=*) printf 'INFO: %s\n' "${line#finding=}" ;;
        count_*) printf 'INFO: DDL %s\n' "${line#count_}" ;;
        *) fail "migration policy wrapper validation failed" ;;
    esac
done <"${VALIDATED_FILE}"
