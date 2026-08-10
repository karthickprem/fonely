"""Schema versions, terminal states, phase catalogue, and validation constants."""

from __future__ import annotations

import hashlib

RUN_MANIFEST_SCHEMA = 1
PHASE_RESULT_SCHEMA = 1
COLLECTION_MANIFEST_SCHEMA = 1
EXECUTION_EVENT_SCHEMA = 1
WAIVER_SCHEMA = 1
TERMINAL_SCHEMA = 1

TERMINAL_SUCCESS = "success"
TERMINAL_TEST_FAILED = "test_failed"
TERMINAL_EVIDENCE_FAILED = "evidence_failed"
TERMINAL_INCOMPLETE = "incomplete"

VALID_TERMINAL_STATES = frozenset({
    TERMINAL_SUCCESS,
    TERMINAL_TEST_FAILED,
    TERMINAL_EVIDENCE_FAILED,
    TERMINAL_INCOMPLETE,
})

VALID_ENVIRONMENTS = frozenset({"ci", "local", "staging"})

VALID_PHASES = (
    "lockfile_check",
    "dependency_install",
    "eval_validation",
    "eval_coverage",
    "package_import",
    "lint",
    "format_check",
    "typecheck",
    "migration_upgrade",
    "migration_check",
    "readiness_tests",
    "readiness_live",
    "verifier_tests",
    "collection_all",
    "collection_non_pg",
    "collection_pg",
    "test_non_pg",
    "test_pg",
    "migration_downgrade",
    "migration_reupgrade",
    "backup_tests",
    "backup_live",
)

VALID_EVENT_PHASES = frozenset({"setup", "call", "teardown"})
VALID_EVENT_OUTCOMES = frozenset({"passed", "failed", "skipped"})

WAIVER_EXCEPTION_CLASSES = frozenset({"setup_skip", "call_xfail"})

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_NODES = 100_000
MAX_WAIVER_LIFETIME_DAYS = 14

RUN_MANIFEST_FILE = "run-manifest.json"
PHASE_RESULTS_FILE = "phase-results.jsonl"
TERMINAL_FILE = "terminal.json"


def collection_manifest_file(partition: str) -> str:
    return f"collection-{partition}.json"


def event_stream_file(partition: str) -> str:
    return f"events-{partition}.jsonl"


def digest_nodes(nodes: list[str] | set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
