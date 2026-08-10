"""Pytest evidence plugin — Authority 2.

Explicitly loaded with -p ci_evidence.pytest_plugin.
Owns: collection manifests, execution event streams, final partition records.
Never writes run-phase state or terminal state.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from ci_evidence.schemas import (
    COLLECTION_MANIFEST_SCHEMA,
    EXECUTION_EVENT_SCHEMA,
    RUN_MANIFEST_FILE,
    RUN_MANIFEST_SCHEMA,
    collection_manifest_file,
    digest_bytes,
    digest_nodes,
    event_stream_file,
    execution_manifest_file,
)
from ci_evidence.writer import TrustedRoot, append_jsonl, atomic_replace_json, safe_read


class _EvidenceState:
    def __init__(self, root: TrustedRoot, manifest: dict[str, Any], partition: str) -> None:
        self.root = root
        self.manifest = manifest
        self.partition = partition
        self.sequence = 0
        self.nodes: list[str] = []
        self.node_markers: dict[str, list[str]] = {}
        self.stream_bytes = b""
        self.collect_only = False


_state: _EvidenceState | None = None


def _get_evidence_root() -> str | None:
    return os.environ.get("FONELY_EVIDENCE_ROOT")


def _get_partition() -> str:
    return os.environ.get("FONELY_EVIDENCE_PARTITION", "non_pg")


def pytest_configure(config: pytest.Config) -> None:
    global _state
    evidence_root = _get_evidence_root()
    if not evidence_root:
        return

    partition = _get_partition()
    root = TrustedRoot(evidence_root)

    raw = safe_read(root, RUN_MANIFEST_FILE)
    manifest = json.loads(raw)
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA:
        raise pytest.UsageError("invalid run manifest schema")

    _state = _EvidenceState(root, manifest, partition)
    _state.collect_only = config.option.collectonly


def pytest_collection_finish(session: pytest.Session) -> None:
    if _state is None:
        return

    nodes = []
    node_markers: dict[str, list[str]] = {}
    for item in session.items:
        node_id = item.nodeid
        nodes.append(node_id)
        markers = [m.name for m in item.iter_markers()]
        node_markers[node_id] = markers

    _state.nodes = nodes
    _state.node_markers = node_markers

    requires_call = {}
    for node_id in nodes:
        markers = node_markers.get(node_id, [])
        requires_call[node_id] = "postgres" in markers

    collection = {
        "schema_version": COLLECTION_MANIFEST_SCHEMA,
        "source_sha": _state.manifest["source_sha"],
        "environment": _state.manifest["environment"],
        "partition": _state.partition,
        "nodes": nodes,
        "requires_call_body": requires_call,
        "node_count": len(nodes),
        "nodes_digest": digest_nodes(nodes),
    }

    target_file = (
        execution_manifest_file(_state.partition)
        if not _state.collect_only
        else collection_manifest_file(_state.partition)
    )
    atomic_replace_json(_state.root, target_file, collection)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _state is None or _state.collect_only:
        return

    if not any(name == "node_id" for name, _ in report.user_properties):
        report.user_properties.append(("node_id", report.nodeid))

    _state.sequence += 1
    event = {
        "schema_version": EXECUTION_EVENT_SCHEMA,
        "node_id": report.nodeid,
        "sequence": _state.sequence,
        "phase": report.when,
        "outcome": report.outcome,
        "wasxfail": getattr(report, "wasxfail", None),
    }

    line = json.dumps(event, sort_keys=True) + "\n"
    _state.stream_bytes += line.encode("utf-8")

    append_jsonl(
        _state.root,
        event_stream_file(_state.partition),
        event,
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _state is None or _state.collect_only:
        _cleanup()
        return

    _state.sequence += 1
    final_record = {
        "schema_version": EXECUTION_EVENT_SCHEMA,
        "record_type": "final",
        "partition": _state.partition,
        "source_sha": _state.manifest["source_sha"],
        "environment": _state.manifest["environment"],
        "final_sequence": _state.sequence,
        "selected_nodes_digest": digest_nodes(_state.nodes),
        "preceding_stream_digest": digest_bytes(_state.stream_bytes),
        "pytest_exit_status": exitstatus,
    }

    append_jsonl(
        _state.root,
        event_stream_file(_state.partition),
        final_record,
    )

    _cleanup()


def _cleanup() -> None:
    global _state
    if _state is not None:
        _state.root.close()
        _state = None
