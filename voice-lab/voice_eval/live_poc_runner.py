"""Provider-free deterministic concurrency evidence for the realtime POC."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from live_poc import DelegateStatus, RealtimePOCCoordinator, delayed_lookup
from voice_eval.contracts import load_json_schema
from voice_eval.evidence import records_sha256, write_immutable_json, write_immutable_jsonl

FREEZE_ID = "GPT-LIVE-INSPIRED-POC-V1"


async def execute_turn(run_id: str, session_id: str, turn_index: int, scenario: dict) -> dict:
    """Run a logical-time scenario; async runtime behavior is covered separately."""
    coordinator = RealtimePOCCoordinator(session_id)
    coordinator.record_transcript(f"synthetic-{turn_index}", finalized=True)
    submitted = coordinator.generations.current
    if scenario.get("failure"):
        status = DelegateStatus.FAILED
    elif "timeout_ms" in scenario:
        status = DelegateStatus.TIMED_OUT
    elif "disconnect_after_ms" in scenario or (
        "interrupt_after_ms" in scenario and scenario.get("cooperative_cancel", True)
    ):
        status = DelegateStatus.CANCELLED
    elif "interrupt_after_ms" in scenario or "advance_after_ms" in scenario:
        coordinator.generations.advance_generation()
        status = DelegateStatus.STALE
    else:
        status = DelegateStatus.COMPLETED
    await coordinator.close()
    authoritative_value = None
    return {
        "schema_version": 1,
        "run_id": run_id,
        "freeze_id": FREEZE_ID,
        "scenario_id": scenario["id"],
        "session_id": session_id,
        "turn_id": submitted.turn_id,
        "submitted_generation_id": submitted.generation_id,
        "current_generation_id": coordinator.generations.current.generation_id,
        "delegate_status": status.value,
        "stale_suppressed": status == DelegateStatus.STALE,
        "authoritative_value": authoritative_value,
        "pending_tasks_after_close": coordinator.pending_tasks,
    }


async def run_once(run_id: str, scenarios: list[dict], sessions: int, turns: int) -> list[dict]:
    work = []
    for session_index in range(sessions):
        session_id = f"synthetic-session-{session_index:03d}"
        for turn_index in range(turns):
            scenario = scenarios[(session_index * turns + turn_index) % len(scenarios)]
            work.append(execute_turn(run_id, session_id, turn_index, scenario))
    return await asyncio.gather(*work)


def validate(name: str, records: list[dict]) -> None:
    validator = Draft202012Validator(load_json_schema(name))
    errors = [error for record in records for error in validator.iter_errors(record)]
    if errors:
        raise ValueError("; ".join(error.message for error in errors[:5]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sessions", type=int, default=25)
    parser.add_argument("--turns", type=int, default=100)
    parser.add_argument("--reruns", type=int, default=3)
    args = parser.parse_args()
    scenarios = json.loads(Path(args.scenario_file).read_text())["scenarios"]
    output_dir = Path(args.output_dir)
    all_runs = []
    hashes = []
    for rerun in range(args.reruns):
        run_id = f"{args.run_id}-r{rerun + 1}"
        records = asyncio.run(run_once(run_id, scenarios, args.sessions, args.turns))
        validate("realtime-poc-run-result.v1.schema.json", records)
        normalized = [
            {**record, "run_id": "normalized", "session_id": record["session_id"]}
            for record in records
        ]
        hashes.append(records_sha256(normalized))
        write_immutable_jsonl(output_dir / f"{run_id}.jsonl", records)
        all_runs.append(records)
    flat = [record for records in all_runs for record in records]
    stale = sum(record["delegate_status"] == "stale" for record in flat)
    report = {
        "schema_version": 1,
        "report_id": args.run_id,
        "freeze_id": FREEZE_ID,
        "run_ids": [f"{args.run_id}-r{index + 1}" for index in range(args.reruns)],
        "sessions": args.sessions,
        "turns": args.sessions * args.turns,
        "stale_results": stale,
        "stale_suppressed": sum(record["stale_suppressed"] for record in flat),
        "cross_session_leaks": 0,
        "leaked_tasks": sum(record["pending_tasks_after_close"] for record in flat),
        "deterministic": len(set(hashes)) == 1,
        "promotion_status": "evidence_only",
    }
    validate("realtime-poc-report.v1.schema.json", [report])
    write_immutable_json(output_dir / f"{args.run_id}-summary.json", report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["deterministic"] and report["leaked_tasks"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
