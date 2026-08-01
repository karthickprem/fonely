#!/usr/bin/env bash
#
# Offline migration smoke-test — no live database required.
#
# Checks:
#   - Alembic history and heads
#   - Exactly one head
#   - Upgrade/downgrade SQL rendering
#   - Migration source quality (empty bodies, extensions, missing metadata)
#   - Migration files match Alembic history
#   - DDL operation counts

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_ROOT="${BACKEND_ROOT:-${PROJECT_ROOT}/backend}"
VENV_BIN="${VENV_BIN:-${BACKEND_ROOT}/.venv/bin}"
ALEMBIC="${ALEMBIC:-${VENV_BIN}/alembic}"
VERSIONS_DIR="${VERSIONS_DIR:-${BACKEND_ROOT}/migrations/versions}"
MIGRATION_POLICY="${SCRIPT_DIR}/migration_policy.py"

FAKE_URL="postgresql+asyncpg://fake_test_user:fake_test_password@localhost:55432/fonely_test"
export DATABASE_URL="${FAKE_URL}"

ERRORS=0
WARNINGS=0

error() { echo "ERROR: $*" >&2; ERRORS=$((ERRORS + 1)); }
warn()  { echo "WARNING: $*" >&2; WARNINGS=$((WARNINGS + 1)); }
info()  { echo "INFO: $*"; }

# ---------------------------------------------------------------------------
# Secure temporary directory
# ---------------------------------------------------------------------------

TMPDIR_WORK="$(mktemp -d)"
cleanup() {
    rm -rf "${TMPDIR_WORK}"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Alembic history and heads
# ---------------------------------------------------------------------------

info "=== Alembic history ==="
cd "${BACKEND_ROOT}"

HISTORY=$("${ALEMBIC}" history 2>&1) || { error "alembic history failed"; }
echo "${HISTORY}"

echo ""
info "=== Alembic heads ==="
HEADS=$("${ALEMBIC}" heads 2>&1) || { error "alembic heads failed"; }
echo "${HEADS}"

HEAD_COUNT=$(echo "${HEADS}" | grep -c "(head)" || true)
if [[ ${HEAD_COUNT} -ne 1 ]]; then
    error "Expected exactly 1 head, found ${HEAD_COUNT}."
else
    info "Single head confirmed."
fi

# ---------------------------------------------------------------------------
# 2. Discover revisions from Alembic (not filenames)
# ---------------------------------------------------------------------------

# Extract the ordered revision chain from Alembic history.
# `alembic history` outputs lines like:
#   0001 -> 0002 (head), pending_action_state_machine
#   <base> -> 0001, initial_schema
# We parse both source and target revision IDs, excluding <base>.
# Then topologically sort: walk from base upward.

declare -A DOWN_TO_UP
declare -A UP_TO_DOWN
ALEMBIC_REVISIONS=()

while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    src=$(echo "${line}" | sed -n 's/^\([^ ]*\) -> .*/\1/p')
    dst=$(echo "${line}" | sed -n 's/^[^ ]* -> \([^ ,()]*\).*/\1/p')
    if [[ -n "${src}" && -n "${dst}" ]]; then
        DOWN_TO_UP["${src}"]="${dst}"
        UP_TO_DOWN["${dst}"]="${src}"
    fi
done <<< "${HISTORY}"

# Walk the chain from <base> upward
CURRENT="<base>"
while [[ -n "${DOWN_TO_UP[${CURRENT}]+_}" ]]; do
    NEXT="${DOWN_TO_UP[${CURRENT}]}"
    ALEMBIC_REVISIONS+=("${NEXT}")
    CURRENT="${NEXT}"
done

if [[ ${#ALEMBIC_REVISIONS[@]} -eq 0 ]]; then
    error "Could not derive any revisions from Alembic history."
fi

info "Revision chain from Alembic: ${ALEMBIC_REVISIONS[*]}"

# Build a set of Alembic-known revisions for cross-referencing
declare -A ALEMBIC_REV_SET
for rev in "${ALEMBIC_REVISIONS[@]}"; do
    ALEMBIC_REV_SET["${rev}"]=1
done

# ---------------------------------------------------------------------------
# 3. Verify migration files match Alembic history
# ---------------------------------------------------------------------------

echo ""
info "=== Migration file / Alembic history cross-check ==="

declare -A FILE_REV_BY_PATH
declare -A REVISION_FILE

for f in "${VERSIONS_DIR}"/*.py; do
    [[ "$(basename "${f}")" == "__init__.py" ]] && continue
    BASENAME="$(basename "${f}")"

    FILE_REV=$(MIGRATION_FILE="${f}" "${VENV_BIN}/python" - <<'EXTRACT_REV_PY' 2>/dev/null || true
import ast
import os
import sys

with open(os.environ["MIGRATION_FILE"]) as source_file:
    tree = ast.parse(source_file.read())
for node in ast.iter_child_nodes(tree):
    target = value = None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        target, value = node.target.id, node.value
    elif isinstance(node, ast.Assign):
        for candidate in node.targets:
            if isinstance(candidate, ast.Name) and candidate.id == "revision":
                target, value = candidate.id, node.value
    if target == "revision" and isinstance(value, ast.Constant) and isinstance(value.value, str):
        print(value.value)
        sys.exit(0)
print("")
EXTRACT_REV_PY
)

    if [[ -z "${FILE_REV}" ]]; then
        warn "${BASENAME}: could not extract revision ID from source."
        continue
    fi

    FILE_REV_BY_PATH["${f}"]="${FILE_REV}"
    REVISION_FILE["${FILE_REV}"]="${BASENAME}"

    if [[ -z "${ALEMBIC_REV_SET[${FILE_REV}]+_}" ]]; then
        error "${BASENAME}: revision '${FILE_REV}' is not in Alembic history (orphan migration file)."
    else
        info "  ${BASENAME}: revision '${FILE_REV}' matches Alembic history."
    fi
done

# ---------------------------------------------------------------------------
# 4. Render cumulative offline upgrade SQL
# ---------------------------------------------------------------------------

echo ""
info "=== Cumulative upgrade SQL (base -> head) ==="
UPGRADE_SQL="${TMPDIR_WORK}/upgrade_full.sql"
if "${ALEMBIC}" upgrade head --sql > "${UPGRADE_SQL}" 2>&1; then
    info "Upgrade SQL rendered to ${UPGRADE_SQL}"
    wc -l < "${UPGRADE_SQL}" | xargs -I{} echo "  Lines: {}"
else
    error "Failed to render cumulative upgrade SQL."
fi

# ---------------------------------------------------------------------------
# 5. Render per-revision downgrade SQL
# ---------------------------------------------------------------------------

echo ""
info "=== Downgrade SQL ranges ==="
PREV="base"
for rev in "${ALEMBIC_REVISIONS[@]}"; do
    DOWNGRADE_SQL="${TMPDIR_WORK}/downgrade_${rev}_to_${PREV}.sql"
    if "${ALEMBIC}" downgrade "${rev}:${PREV}" --sql > "${DOWNGRADE_SQL}" 2>&1; then
        info "  ${rev} -> ${PREV}: rendered ($(wc -l < "${DOWNGRADE_SQL}" | xargs) lines)"
    else
        error "Failed to render downgrade SQL for ${rev} -> ${PREV}."
    fi
    PREV="${rev}"
done

# ---------------------------------------------------------------------------
# 6. Check migration source files
# ---------------------------------------------------------------------------

echo ""
info "=== Migration source checks ==="

declare -A EXTENSION_REVISION_SET

for f in "${VERSIONS_DIR}"/*.py; do
    [[ "$(basename "${f}")" == "__init__.py" ]] && continue
    BASENAME="$(basename "${f}")"
    info "Checking ${BASENAME}..."

    if ! grep -qP '^revision\s*[:=]' "${f}"; then
        error "${BASENAME}: missing 'revision' attribute."
    fi

    if ! grep -qP '^down_revision\s*[:=]' "${f}"; then
        error "${BASENAME}: missing 'down_revision' attribute."
    fi

    UPGRADE_BODY=$(MIGRATION_FILE="${f}" "${VENV_BIN}/python" - <<'CHECK_UPGRADE_PY' 2>&1
import ast, os, sys
fpath = os.environ["MIGRATION_FILE"]
with open(fpath) as fh:
    tree = ast.parse(fh.read())
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
        body = [n for n in node.body if not isinstance(n, (ast.Pass, ast.Expr)) or
                (isinstance(n, ast.Expr) and not isinstance(n.value, (ast.Constant, ast.Str)))]
        print(len(body))
        sys.exit(0)
print(-1)
CHECK_UPGRADE_PY
)

    if [[ "${UPGRADE_BODY}" == "0" ]]; then
        error "${BASENAME}: upgrade() body is empty."
    fi

    DOWNGRADE_BODY=$(MIGRATION_FILE="${f}" "${VENV_BIN}/python" - <<'CHECK_DOWNGRADE_PY' 2>&1
import ast, os, sys
fpath = os.environ["MIGRATION_FILE"]
with open(fpath) as fh:
    tree = ast.parse(fh.read())
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
        body = [n for n in node.body if not isinstance(n, (ast.Pass, ast.Expr)) or
                (isinstance(n, ast.Expr) and not isinstance(n.value, (ast.Constant, ast.Str)))]
        print(len(body))
        sys.exit(0)
print(-1)
CHECK_DOWNGRADE_PY
)

    if [[ "${DOWNGRADE_BODY}" == "0" ]]; then
        error "${BASENAME}: downgrade() body is empty."
    fi

    SOURCE_STATUS=0
    SOURCE_SCAN=$("${VENV_BIN}/python" "${MIGRATION_POLICY}" source "${f}" 2>&1) || SOURCE_STATUS=$?
    REQUESTED_EXTENSIONS=()
    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        if [[ "${line}" == REQUESTED_EXTENSION=* ]]; then
            REQUESTED_EXTENSIONS+=("${line#REQUESTED_EXTENSION=}")
        elif [[ "${line}" == ERROR:* ]]; then
            error "${line#ERROR: }"
        else
            echo "${line}"
        fi
    done <<< "${SOURCE_SCAN}"
    if [[ ${SOURCE_STATUS} -ne 0 && "${SOURCE_SCAN}" != *"ERROR:"* ]]; then
        error "${BASENAME}: extension source scan failed."
    fi
    if [[ ${#REQUESTED_EXTENSIONS[@]} -gt 0 ]]; then
        FILE_REV="${FILE_REV_BY_PATH[${f}]:-}"
        if [[ -z "${FILE_REV}" ]]; then
            error "${BASENAME}: extension migration has no literal revision ID."
        else
            EXTENSION_REVISION_SET["${FILE_REV}"]="${REQUESTED_EXTENSIONS[*]}"
        fi
    fi
done

# ---------------------------------------------------------------------------
# 7. Verify rendered SQL for extension revisions
# ---------------------------------------------------------------------------

echo ""
info "=== Rendered extension revision SQL policy ==="

for rev in "${ALEMBIC_REVISIONS[@]}"; do
    [[ -z "${EXTENSION_REVISION_SET[${rev}]+_}" ]] && continue
    down_revision="${UP_TO_DOWN[${rev}]}"
    [[ "${down_revision}" == "<base>" ]] && down_revision="base"
    SAFE_REV_NAME=$(printf '%s' "${rev}" | tr -c 'A-Za-z0-9_.-' '_')
    REV_UPGRADE_SQL="${TMPDIR_WORK}/upgrade_${SAFE_REV_NAME}.sql"
    REV_DOWNGRADE_SQL="${TMPDIR_WORK}/downgrade_${SAFE_REV_NAME}.sql"
    if ! "${ALEMBIC}" upgrade "${down_revision}:${rev}" --sql > "${REV_UPGRADE_SQL}" 2>&1; then
        error "${rev}: failed to render exact extension upgrade range."
        continue
    fi
    if ! "${ALEMBIC}" downgrade "${rev}:${down_revision}" --sql > "${REV_DOWNGRADE_SQL}" 2>&1; then
        error "${rev}: failed to render exact extension downgrade range."
        continue
    fi
    POLICY_ARGS=(
        rendered
        --upgrade-sql "${REV_UPGRADE_SQL}"
        --downgrade-sql "${REV_DOWNGRADE_SQL}"
        --migration-name "${REVISION_FILE[${rev}]}"
        --revision "${rev}"
    )
    for requested_extension in ${EXTENSION_REVISION_SET[${rev}]}; do
        POLICY_ARGS+=(--requested-extension "${requested_extension}")
    done
    RENDER_STATUS=0
    RENDER_SCAN=$("${VENV_BIN}/python" "${MIGRATION_POLICY}" "${POLICY_ARGS[@]}" 2>&1) || RENDER_STATUS=$?
    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        if [[ "${line}" == ERROR:* ]]; then error "${line#ERROR: }"; else echo "${line}"; fi
    done <<< "${RENDER_SCAN}"
    if [[ ${RENDER_STATUS} -ne 0 && "${RENDER_SCAN}" != *"ERROR:"* ]]; then
        error "${rev}: rendered extension SQL scan failed."
    fi
done

# ---------------------------------------------------------------------------
# 8. DDL operation counts from upgrade SQL
# ---------------------------------------------------------------------------

echo ""
info "=== DDL operation counts (from upgrade SQL) ==="

if [[ -f "${UPGRADE_SQL}" ]]; then
    count_pattern() {
        local label="$1"
        local pattern="$2"
        local count
        count=$(grep -ciP "${pattern}" "${UPGRADE_SQL}" || true)
        printf "  %-25s %d\n" "${label}:" "${count}"
    }

    count_pattern "CREATE TABLE"    'CREATE\s+TABLE'
    count_pattern "DROP TABLE"      'DROP\s+TABLE'
    count_pattern "ADD COLUMN"      'ADD\s+COLUMN'
    count_pattern "DROP COLUMN"     'DROP\s+COLUMN'
    count_pattern "CHECK constraint" 'CHECK\s*\('
    count_pattern "CREATE INDEX"    'CREATE\s+INDEX'
    count_pattern "DROP INDEX"      'DROP\s+INDEX'
else
    warn "Upgrade SQL file not available for DDL counts."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "========================================"
if [[ ${ERRORS} -gt 0 ]]; then
    echo "FAILED: ${ERRORS} error(s), ${WARNINGS} warning(s)."
    exit 1
else
    echo "PASSED: 0 errors, ${WARNINGS} warning(s)."
    exit 0
fi
