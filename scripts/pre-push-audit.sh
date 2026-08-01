#!/usr/bin/env bash
# Read-only repository audit for working-tree, staged, or Git revision-range content.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MAX_BYTES=$((10 * 1024 * 1024))
TMPDIR_WORK="$(mktemp -d)"

cleanup() {
    rm -rf "${TMPDIR_WORK}"
}
trap cleanup EXIT

resolve_python() {
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
    elif command -v python >/dev/null 2>&1; then
        command -v python
    else
        echo "ERROR: Python 3 or Python is required for the audit." >&2
        exit 2
    fi
}

PYTHON_BIN="$(resolve_python)"
MODE=""
REVISION_RANGE=""

usage() {
    cat <<'USAGE'
Usage:
  scripts/pre-push-audit.sh --working-tree
  scripts/pre-push-audit.sh --staged
  scripts/pre-push-audit.sh --range <revision-range>

Modes:
  --working-tree  Audit tracked/untracked, non-ignored working-tree candidates.
  --staged        Audit index blobs exactly as they would be committed.
  --range RANGE   Audit every introduced/modified blob in every commit in RANGE.

In a repository with commits, an explicit mode is required. Before Git
initialization, no arguments defaults to --working-tree.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --working-tree)
            [[ -z "${MODE}" ]] || { echo "ERROR: Choose exactly one audit mode." >&2; exit 2; }
            MODE="working-tree"
            shift
            ;;
        --staged)
            [[ -z "${MODE}" ]] || { echo "ERROR: Choose exactly one audit mode." >&2; exit 2; }
            MODE="staged"
            shift
            ;;
        --range)
            [[ -z "${MODE}" ]] || { echo "ERROR: Choose exactly one audit mode." >&2; exit 2; }
            [[ $# -ge 2 && -n "$2" ]] || { echo "ERROR: --range requires a revision range." >&2; exit 2; }
            MODE="range"
            REVISION_RANGE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

IS_GIT_REPOSITORY=0
HAS_HEAD=0
if git -C "${PROJECT_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    IS_GIT_REPOSITORY=1
    if git -C "${PROJECT_ROOT}" rev-parse --verify HEAD^{commit} >/dev/null 2>&1; then
        HAS_HEAD=1
    fi
fi

if [[ -z "${MODE}" ]]; then
    if [[ ${IS_GIT_REPOSITORY} -eq 0 || ${HAS_HEAD} -eq 0 ]]; then
        MODE="working-tree"
    else
        echo "ERROR: Explicit --working-tree, --staged, or --range is required in a Git repository with commits." >&2
        exit 2
    fi
fi

if [[ "${MODE}" != "working-tree" && ${IS_GIT_REPOSITORY} -eq 0 ]]; then
    echo "ERROR: --${MODE} requires an initialized Git repository." >&2
    exit 2
fi

if [[ "${MODE}" == "range" ]]; then
    if ! git -C "${PROJECT_ROOT}" rev-list --reverse "${REVISION_RANGE}" \
        > "${TMPDIR_WORK}/range-commits" 2> "${TMPDIR_WORK}/range-error"; then
        echo "ERROR: Invalid or ambiguous revision range." >&2
        exit 2
    fi
fi

export FONELY_AUDIT_ROOT="${PROJECT_ROOT}"
export FONELY_AUDIT_MODE="${MODE}"
export FONELY_AUDIT_RANGE="${REVISION_RANGE}"
export FONELY_AUDIT_TEMP="${TMPDIR_WORK}"
export FONELY_AUDIT_MAX_BYTES="${MAX_BYTES}"

"${PYTHON_BIN}" <<'PY'
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

ROOT = Path(os.environ["FONELY_AUDIT_ROOT"])
MODE = os.environ["FONELY_AUDIT_MODE"]
REVISION_RANGE = os.environ["FONELY_AUDIT_RANGE"]
TEMP = Path(os.environ["FONELY_AUDIT_TEMP"])
MAX_BYTES = int(os.environ["FONELY_AUDIT_MAX_BYTES"])
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass(frozen=True)
class Candidate:
    path: str
    source: str
    blob_oid: str | None = None
    commit: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class Finding:
    category: str
    path: str
    commit: str | None = None


def git(*args: str, input_data: bytes | None = None, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError("Git object operation failed")
    return result.stdout


def quoted_path(path: str) -> str:
    return json.dumps(path, ensure_ascii=True)


def forbidden_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    if name == ".env.example":
        return False
    if name == ".env" or ".env" in name and name.startswith(".env."):
        return True
    forbidden_dirs = {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "test_output",
        "voice_samples",
        "demo_audio",
        "results",
        ".ssh",
    }
    if any(part in forbidden_dirs for part in parts):
        if "results" not in parts or "evals" in parts:
            return True
    lower_name = name.lower()
    forbidden_suffixes = (
        ".pyc",
        ".pyo",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".log",
        ".pem",
        ".key",
        ".wav",
        ".mp3",
        ".raw",
        ".pcm",
    )
    return lower_name.startswith("id_ed25519") or lower_name.endswith(forbidden_suffixes)


PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("sarvam-api-key", re.compile(r"\bsk_[A-Za-z0-9_-]{20,}\b"), 0),
    ("generic-sk-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), 0),
    ("github-classic-pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), 0),
    ("github-fine-grained-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"), 0),
    ("aws-access-key", re.compile(r"\bAKIA[A-Z0-9]{16}\b"), 0),
    ("bearer-token", re.compile(r"(?i)\bBearer[ \t]+([A-Za-z0-9._~+/-]{16,}={0,2})"), 1),
    ("private-key-header", re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"), 0),
    (
        "exotel-credential",
        re.compile(
            r"(?im)^\s*EXOTEL_[A-Z_]*(?:KEY|TOKEN|SECRET)[ \t]*=[ \t]*['\"]?([^\s'\"]+)"
        ),
        1,
    ),
    (
        "amd-gateway-key",
        re.compile(
            r"(?im)^\s*(?:Ocp-Apim-Subscription-Key|AMD_LLM_API_KEY)"
            r"[ \t]*(?:=|:)[ \t]*['\"]?([^\s'\"]+)"
        ),
        1,
    ),
    (
        "credentialed-database-url",
        re.compile(
            r"(?i)\bpostgres(?:ql)?(?:\+asyncpg)?://[^\s:/\"'<>]+:"
            r"[^\s@\"'<>]+@[^\s\"'<>]+"
        ),
        0,
    ),
)

EXACT_PLACEHOLDERS = {
    "YOUR_TOKEN_HERE",
    "YOUR_API_KEY_HERE",
    "YOUR_SECRET_HERE",
    "${TOKEN}",
    "${API_KEY}",
    "${SECRET}",
    "<TOKEN>",
    "<API_KEY>",
    "<SECRET>",
    "sk_xxxxx",
    "sk_your_key_here",
    "your_api_key",
    "your_api_token",
    "your_account_sid",
}

APPROVED_DATABASE_URLS: dict[str, set[str]] = {
    "backend/.env.example": {
        "postgresql+asyncpg://user:password@localhost:5432/fonely",
    },
    ".github/workflows/backend-ci.yml": {
        "postgresql+asyncpg://fonely_test:fonely_test_ci_only@localhost:5432/fonely_test",
    },
    "docs/testing/POSTGRESQL.md": {
        "postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test",
    },
    "infra/postgres/compose.yaml": {
        "postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test",
    },
    "scripts/check-migrations.sh": {
        "postgresql+asyncpg://fake_test_user:fake_test_password@localhost:55432/fonely_test",
    },
    "scripts/test-postgres.sh": {
        "postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test",
    },
    "scripts/pre-push-audit.sh": {
        "postgresql+asyncpg://user:password@localhost:5432/fonely",
        "postgresql+asyncpg://fonely_test:fonely_test_ci_only@localhost:5432/fonely_test",
        "postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test",
        "postgresql+asyncpg://fake_test_user:fake_test_password@localhost:55432/fonely_test",
    },
}

APPROVED_DATABASE_CREDENTIALS_BY_PATH: dict[str, set[tuple[str, str]]] = {
    "backend/tests/unit/pending_actions/test_postgres_safety.py": {
        ("fonely_test_user", "secret"),
        ("app_user", "secret"),
    },
}


def normalize_database_match(value: str) -> str:
    return value.rstrip("),.;]}")


def is_exact_placeholder(value: str) -> bool:
    return value.strip("'\"") in EXACT_PLACEHOLDERS


def approved_database(path: str, value: str) -> bool:
    normalized = normalize_database_match(value)
    if normalized in APPROVED_DATABASE_URLS.get(path, set()):
        return True
    try:
        parsed = urlparse(normalized.replace("+asyncpg", ""))
    except ValueError:
        return False
    credentials = ((parsed.username or ""), (parsed.password or ""))
    return credentials in APPROVED_DATABASE_CREDENTIALS_BY_PATH.get(path, set())


def scan_text(path: str, text: str) -> set[str]:
    categories: set[str] = set()
    for category, pattern, group in PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(group)
            if is_exact_placeholder(value):
                continue
            if category == "credentialed-database-url" and approved_database(path, value):
                continue
            categories.add(category)
    return categories


def is_text(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def validate_relative_path(path: str) -> None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise RuntimeError("Candidate path escapes repository")


def working_tree_candidates() -> list[Candidate]:
    if (ROOT / ".git").exists() or git("rev-parse", "--is-inside-work-tree", check=False).strip() == b"true":
        raw = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    else:
        with tempfile.TemporaryDirectory(prefix="fonely-audit-git-", dir=TEMP) as git_dir:
            subprocess.run(
                ["git", "init", "-q", git_dir],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result = subprocess.run(
                [
                    "git",
                    f"--git-dir={Path(git_dir) / '.git'}",
                    f"--work-tree={ROOT}",
                    "-c",
                    "core.excludesFile=/dev/null",
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            raw = result.stdout
    return [
        Candidate(path=value.decode("utf-8", errors="surrogateescape"), source="working-tree")
        for value in raw.split(b"\0")
        if value
    ]


def staged_candidates() -> list[Candidate]:
    visible_raw = git(
        "diff",
        "--cached",
        "--ita-visible-in-index",
        "--name-only",
        "--diff-filter=AM",
        "-z",
    )
    normal_raw = git(
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=AM",
        "-z",
    )
    changed = {
        value.decode("utf-8", errors="surrogateescape")
        for value in visible_raw.split(b"\0")
        if value
    }
    normal = {
        value.decode("utf-8", errors="surrogateescape")
        for value in normal_raw.split(b"\0")
        if value
    }
    intent_to_add = changed - normal
    raw = git("ls-files", "--stage", "-z")
    candidates: list[Candidate] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        path = path_bytes.decode("utf-8", errors="surrogateescape")
        if path not in changed:
            continue
        if stage != "0":
            candidates.append(Candidate(path=path, source="unmerged-index", mode=mode))
        elif path in intent_to_add or set(oid) == {"0"}:
            candidates.append(Candidate(path=path, source="intent-to-add", mode=mode))
        else:
            candidates.append(Candidate(path=path, source="staged", blob_oid=oid, mode=mode))
    return candidates


def tree_blob(commit: str, path: str) -> tuple[str, str] | None:
    raw = git("ls-tree", "-z", commit, "--", f":(literal){path}")
    if not raw:
        return None
    metadata, _ = raw.split(b"\t", 1)
    mode, object_type, oid = metadata.decode("ascii").split()
    if object_type != "blob":
        return None
    return mode, oid


def range_candidates() -> list[Candidate]:
    commits = [line for line in (TEMP / "range-commits").read_text().splitlines() if line]
    candidates: list[Candidate] = []
    for commit in commits:
        parent_line = git("rev-list", "--parents", "-n", "1", commit).decode("ascii").strip()
        fields = parent_line.split()
        parents = fields[1:] or [EMPTY_TREE]
        changed_paths: set[str] = set()
        for parent in parents:
            raw = git(
                "diff-tree",
                "--no-commit-id",
                "--no-renames",
                "--diff-filter=AM",
                "-r",
                "--name-only",
                "-z",
                parent,
                commit,
            )
            changed_paths.update(
                value.decode("utf-8", errors="surrogateescape")
                for value in raw.split(b"\0")
                if value
            )
        for path in sorted(changed_paths):
            blob = tree_blob(commit, path)
            if blob is None:
                continue
            mode, oid = blob
            candidates.append(
                Candidate(
                    path=path,
                    source="range",
                    blob_oid=oid,
                    commit=commit,
                    mode=mode,
                )
            )
    return candidates


def read_blob(oid: str) -> bytes:
    return git("cat-file", "blob", oid)


def blob_size(oid: str) -> int:
    return int(git("cat-file", "-s", oid).decode("ascii").strip())


def working_tree_data(candidate: Candidate) -> tuple[bytes, int] | None:
    validate_relative_path(candidate.path)
    pure = PurePosixPath(candidate.path)
    current = ROOT
    final_info: os.stat_result | None = None
    try:
        for index, part in enumerate(pure.parts):
            current = current / part
            info = os.lstat(current)
            is_final = index == len(pure.parts) - 1
            if stat.S_ISLNK(info.st_mode):
                if not is_final:
                    return None
                target = os.readlink(current).encode("utf-8", errors="surrogateescape")
                return target, info.st_size
            if not is_final and not stat.S_ISDIR(info.st_mode):
                return None
            final_info = info
    except FileNotFoundError:
        return None
    if final_info is None or not stat.S_ISREG(final_info.st_mode):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(current, flags)
    except OSError:
        return None
    try:
        opened_info = os.fstat(descriptor)
        if not stat.S_ISREG(opened_info.st_mode):
            return None
        if opened_info.st_size > MAX_BYTES:
            return b"", opened_info.st_size
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), opened_info.st_size
    finally:
        os.close(descriptor)


def finding_line(finding: Finding) -> str:
    location = quoted_path(finding.path)
    if finding.commit:
        return f"FINDING [{finding.category}] commit={finding.commit[:12]} path={location}"
    return f"FINDING [{finding.category}] path={location}"


if MODE == "working-tree":
    candidates = working_tree_candidates()
elif MODE == "staged":
    candidates = staged_candidates()
else:
    candidates = range_candidates()

findings: set[Finding] = set()
scanned_blobs: set[str] = set()
blob_associations: defaultdict[str, list[Candidate]] = defaultdict(list)
working_items: list[tuple[Candidate, bytes, int]] = []

for candidate in candidates:
    validate_relative_path(candidate.path)
    if forbidden_path(candidate.path):
        findings.add(Finding("forbidden-path", candidate.path, candidate.commit))
        continue
    if candidate.source in {"intent-to-add", "unmerged-index"}:
        findings.add(Finding(f"unscannable-{candidate.source}", candidate.path, candidate.commit))
        continue
    if candidate.blob_oid:
        blob_associations[candidate.blob_oid].append(candidate)
    else:
        item = working_tree_data(candidate)
        if item is not None:
            data, size = item
            working_items.append((candidate, data, size))

for candidate, data, size in working_items:
    if size > MAX_BYTES:
        findings.add(Finding("file-over-10MiB", candidate.path, candidate.commit))
    if is_text(data):
        text = data.decode("utf-8")
        for category in scan_text(candidate.path, text):
            findings.add(Finding(category, candidate.path, candidate.commit))

for oid, associations in blob_associations.items():
    if oid in scanned_blobs:
        continue
    scanned_blobs.add(oid)
    size = blob_size(oid)
    data = read_blob(oid)
    for candidate in associations:
        if size > MAX_BYTES:
            findings.add(Finding("file-over-10MiB", candidate.path, candidate.commit))
    if not is_text(data):
        continue
    text = data.decode("utf-8")
    for candidate in associations:
        for category in scan_text(candidate.path, text):
            findings.add(Finding(category, candidate.path, candidate.commit))

for finding in sorted(findings, key=lambda item: (item.commit or "", item.path, item.category)):
    print(finding_line(finding), file=sys.stderr)

print(
    f"Audit mode={MODE}: candidates={len(candidates)}, "
    f"unique_blobs={len(scanned_blobs)}, findings={len(findings)}"
)
if findings:
    print("Repository audit FAILED.", file=sys.stderr)
    raise SystemExit(1)
print("Repository audit PASSED.")
PY
