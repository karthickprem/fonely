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
BACKEND_ROOT="${PROJECT_ROOT}/backend"
VENV_BIN="${BACKEND_ROOT}/.venv/bin"
ALEMBIC="${VENV_BIN}/alembic"
VERSIONS_DIR="${BACKEND_ROOT}/migrations/versions"

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

for f in "${VERSIONS_DIR}"/*.py; do
    [[ "$(basename "${f}")" == "__init__.py" ]] && continue
    BASENAME="$(basename "${f}")"

    FILE_REV=$(MIGRATION_FILE="${f}" "${VENV_BIN}/python" - <<'EXTRACT_REV_PY' 2>/dev/null || true
import ast, os, sys
fpath = os.environ["MIGRATION_FILE"]
with open(fpath) as fh:
    tree = ast.parse(fh.read())
for node in ast.iter_child_nodes(tree):
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.target.id == "revision" and node.value:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                print(node.value.value)
                sys.exit(0)
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "revision":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    print(node.value.value)
                    sys.exit(0)
print("")
EXTRACT_REV_PY
)

    if [[ -z "${FILE_REV}" ]]; then
        warn "${BASENAME}: could not extract revision ID from source."
        continue
    fi

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

ALLOWED_EXTENSIONS="uuid-ossp"

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

    if grep -qiP 'CREATE\s+EXTENSION' "${f}"; then
        EXT_NAME=$(grep -oiP 'CREATE\s+EXTENSION[^"]*"([^"]+)"' "${f}" | head -1 | grep -oP '"[^"]+"' | tr -d '"')
        if [[ -z "${EXT_NAME}" ]]; then
            EXT_NAME=$(grep -oiP "CREATE\s+EXTENSION[^']*'([^']+)'" "${f}" | head -1 | grep -oP "'[^']+'" | tr -d "'")
        fi

        ALLOWED=0
        for allowed_ext in ${ALLOWED_EXTENSIONS}; do
            if [[ "${EXT_NAME}" == "${allowed_ext}" ]]; then
                ALLOWED=1
                break
            fi
        done

        if [[ ${ALLOWED} -eq 0 ]]; then
            error "${BASENAME}: CREATE EXTENSION '${EXT_NAME}' is not in the allowlist (${ALLOWED_EXTENSIONS})."
        else
            info "  CREATE EXTENSION '${EXT_NAME}' is allowlisted."
        fi
    fi
done

# ---------------------------------------------------------------------------
# 7. DDL operation counts from upgrade SQL
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
