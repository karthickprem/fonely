from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .audio import verify_fixture_audio

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


def load_json_schema(name: str) -> dict:
    schema = json.loads((SCHEMA_DIR / name).read_text())
    Draft202012Validator.check_schema(schema)
    return schema


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: record must be an object")
        records.append(value)
    return records


def _validate_records(records: list[dict], schema_name: str) -> None:
    validator = Draft202012Validator(load_json_schema(schema_name))
    for index, record in enumerate(records, 1):
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
        if errors:
            details = "; ".join(f"{'.'.join(map(str, error.path)) or '$'}: {error.message}" for error in errors)
            raise ValueError(f"record {index}: {details}")


def validate_manifest(path: Path, data_root: Path) -> list[dict]:
    root = data_root.resolve(strict=True)
    worktree = Path(__file__).resolve().parents[2]
    if root == worktree or root.is_relative_to(worktree):
        raise ValueError("VOICE_EVAL_DATA_ROOT must be outside the Git worktree")
    manifest_path = path.resolve(strict=True)
    if not manifest_path.is_relative_to(root):
        raise ValueError("manifest must be stored under the evaluation data root")
    records = _read_jsonl(manifest_path)
    if not records:
        return []
    versions = {record.get("schema_version") for record in records}
    if len(versions) != 1 or versions.pop() not in {1, 2}:
        raise ValueError("manifest must contain one supported schema version")
    version = records[0]["schema_version"]
    _validate_records(records, f"audio-fixture-manifest.v{version}.schema.json")
    seen: set[str] = set()
    speakers_by_split: dict[str, set[str]] = {"development": set(), "test": set()}
    for record in records:
        fixture_id = record["fixture_id"]
        if fixture_id in seen:
            raise ValueError(f"duplicate fixture_id: {fixture_id}")
        seen.add(fixture_id)
        source = record["provenance"]["source"]
        if version == 1 and source in {"human_recording", "licensed_external"}:
            raise ValueError(f"{fixture_id}: V1 rejects unapproved real/licensed collection")
        if version == 2:
            approval = root / "governance" / "approvals" / f"{record['governance']['approval_receipt_id']}.json"
            consent = root / "governance" / "consents" / f"{record['governance']['consent_receipt_id']}.json"
            if not approval.exists() or not consent.exists():
                raise ValueError(f"{fixture_id}: missing governance receipt")
            approval_data = json.loads(approval.read_text())
            consent_data = json.loads(consent.read_text())
            if approval_data.get("policy_sha256") != record["governance"]["policy_sha256"] or consent_data.get("consent_sha256") != record["governance"]["consent_sha256"]:
                raise ValueError(f"{fixture_id}: governance receipt hash mismatch")
        verify_fixture_audio(record, root)
        if record.get("speaker_id"):
            speakers_by_split[record["split"]].add(record["speaker_id"])
    overlap = speakers_by_split["development"] & speakers_by_split["test"]
    if overlap:
        raise ValueError(f"speaker split leakage: {sorted(overlap)}")
    return records


def apply_annotations(fixtures: list[dict], data_root: Path) -> list[dict]:
    root = data_root.resolve(strict=True)
    effective = []
    for source in fixtures:
        fixture = source.copy()
        annotation_path = (root / "annotations" / f"{fixture['fixture_id']}.json").resolve(strict=False)
        if annotation_path.exists():
            if not annotation_path.is_relative_to(root):
                raise ValueError("annotation path escapes evaluation data root")
            annotation = json.loads(annotation_path.read_text())
            if annotation.get("fixture_id") != fixture["fixture_id"]:
                raise ValueError("annotation fixture ID mismatch")
            fixture["reference"] = {
                **fixture["reference"],
                "transcript": annotation["transcript"],
                "intent_label": annotation.get("intent"),
            }
            fixture["annotation"] = {
                **fixture["annotation"],
                "revision": annotation["revision"],
                "annotation_status": annotation["review_status"],
            }
        effective.append(fixture)
    return effective


def validate_results(path: Path) -> list[dict]:
    records = _read_jsonl(path)
    _validate_records(records, "voice-run-result.v1.schema.json")
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        key = (record["run_id"], record["fixture_id"], record["provider"]["mode"])
        if key in seen:
            raise ValueError(f"duplicate run result: {key}")
        seen.add(key)
        metrics = record["metrics"]
        if metrics["critical_entity_correct"] > metrics["critical_entity_total"]:
            raise ValueError(f"critical entity counts are inconsistent: {key}")
    return records


def validate_report(record: dict) -> None:
    _validate_records([record], "voice-eval-report.v1.schema.json")
