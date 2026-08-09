"""Shared test configuration and canonical execution-evidence hook."""

import json
import os
from pathlib import Path

import pytest

POSTGRES_AVAILABLE = bool(os.environ.get("FONELY_TEST_DATABASE_URL"))

postgres = pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="FONELY_TEST_DATABASE_URL not set — PostgreSQL tests skipped",
)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Attach canonical node ID and append phase-level execution evidence."""
    if not any(name == "node_id" for name, _ in report.user_properties):
        report.user_properties.append(("node_id", report.nodeid))
    evidence_path = os.environ.get("FONELY_EXECUTION_EVIDENCE_PATH")
    if not evidence_path:
        return
    event = {
        "schema_version": 1,
        "node_id": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "wasxfail": getattr(report, "wasxfail", None),
    }
    path = Path(evidence_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
