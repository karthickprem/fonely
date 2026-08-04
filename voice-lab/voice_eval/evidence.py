"""Immutable evidence writers for frozen STT experiments."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def canonical_json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def records_sha256(records: list[dict]) -> str:
    payload = "".join(canonical_json(record) + "\n" for record in records)
    return hashlib.sha256(payload.encode()).hexdigest()


def write_immutable_jsonl(path: Path, records: list[dict]) -> str:
    """Create a JSONL evidence file atomically and refuse replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(canonical_json(record) + "\n" for record in records)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload.encode()).hexdigest()


def write_immutable_json(path: Path, record: dict) -> str:
    """Create an indented JSON evidence file atomically and refuse replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical = canonical_json(record)
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(canonical.encode()).hexdigest()
