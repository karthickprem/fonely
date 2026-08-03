from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice_eval.audio import read_pcm16_mono, resolve_audio_path
from voice_eval.contracts import apply_annotations, validate_manifest, validate_report, validate_results, _validate_records
from voice_eval.correction import propose_shadow_correction
from voice_eval.metrics import aggregate_results, normalize_transcript, score_critical_entities, word_error_counts
from voice_eval.saaras_runner import transcribe_fixture


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_output_path(data_root: Path, path: Path) -> Path:
    root = data_root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("output must be stored under the evaluation data root")
    return resolved


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records))
    temporary.replace(path)


async def run_saaras(args) -> int:
    data_root = Path(args.data_root)
    fixtures = apply_annotations(validate_manifest(Path(args.manifest), data_root), data_root)
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    if not modes or any(mode not in {"transcribe", "codemix"} for mode in modes):
        raise ValueError("modes must contain transcribe and/or codemix")
    output_path = safe_output_path(data_root, Path(args.output))
    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY is required")
    records = []
    for fixture in fixtures:
        pcm = read_pcm16_mono(resolve_audio_path(data_root, fixture["audio"]["relative_path"]), 16000)
        for mode in modes:
            errors = []
            try:
                observation = await transcribe_fixture(pcm, mode=mode, api_key=api_key)
                errors.extend(observation.errors)
                status = "passed" if observation.transcript and not errors else "provider_error"
            except Exception as exc:
                observation = None
                status = "provider_error"
                errors.append(f"{type(exc).__name__}: {exc}")
            raw = observation.transcript if observation else ""
            correction = propose_shadow_correction(raw, fixture["reference"]["critical_entities"])
            raw_counts = word_error_counts(fixture["reference"]["transcript"], raw, fixture["locale"])
            shadow_counts = word_error_counts(fixture["reference"]["transcript"], correction.proposed_transcript, fixture["locale"])
            entity_correct, entity_total = score_critical_entities(fixture["reference"]["critical_entities"], raw, fixture["locale"])
            shadow_correct, _ = score_critical_entities(fixture["reference"]["critical_entities"], correction.proposed_transcript, fixture["locale"])
            records.append({
                "schema_version": 1,
                "run_id": args.run_id,
                "fixture_id": fixture["fixture_id"],
                "provider": {"name": "sarvam", "model": "saaras:v3", "mode": mode, "configuration": {"sample_rate_hz": 16000, "input_audio_codec": "wav", "vad_signals": False}},
                "input": {"audio_sha256": fixture["audio"]["sha256"], "sample_rate_hz": 16000, "duration_ms": fixture["audio"]["duration_ms"]},
                "output": {"raw_transcript": raw, "provider_language": observation.language if observation else None, "provider_confidence": observation.confidence if observation else None},
                "shadow_correction": {"candidates": correction.candidates, "proposed_transcript": correction.proposed_transcript, "decision": correction.decision, "reasons": correction.reasons, "changed_critical_field": correction.changed_critical_field},
                "timing": {"wall_ms": observation.wall_ms if observation else 0, "provider_ttfb_ms": None},
                "metrics": {"substitutions": raw_counts.substitutions, "insertions": raw_counts.insertions, "deletions": raw_counts.deletions, "reference_words": raw_counts.reference_words, "wer": raw_counts.wer, "critical_entity_correct": entity_correct, "critical_entity_total": entity_total, "raw_exact_match": normalize_transcript(fixture["reference"]["transcript"], fixture["locale"]) == normalize_transcript(raw, fixture["locale"]), "shadow_wer": shadow_counts.wer, "shadow_critical_regression": shadow_correct < entity_correct},
                "status": status,
                "errors": errors,
            })
    _validate_records(records, "voice-run-result.v1.schema.json")
    write_jsonl(output_path, records)
    print(f"Wrote {len(records)} result rows to {args.output}")
    return 0


def build_report(args) -> int:
    manifest_path = Path(args.manifest)
    fixtures = validate_manifest(manifest_path, Path(args.data_root))
    results = validate_results(Path(args.results))
    fixture_map = {fixture["fixture_id"]: fixture for fixture in fixtures}
    for result in results:
        fixture = fixture_map.get(result["fixture_id"])
        if fixture is None:
            raise ValueError(f"result references unknown fixture: {result['fixture_id']}")
        if result["input"]["audio_sha256"] != fixture["audio"]["sha256"]:
            raise ValueError(f"result audio hash does not match manifest: {result['fixture_id']}")
    output_path = safe_output_path(Path(args.data_root), Path(args.output))
    aggregate = aggregate_results(results, fixture_map)
    expected_modes = {mode.strip() for mode in args.expected_modes.split(",") if mode.strip()}
    coverage_complete = all(
        {
            result["provider"]["mode"]
            for result in results
            if result["run_id"] == run_id and result["fixture_id"] == fixture_id
        } == expected_modes
        for run_id in {result["run_id"] for result in results}
        for fixture_id in fixture_map
    )
    report = {
        "schema_version": 1,
        "report_id": args.report_id,
        "run_ids": sorted({result["run_id"] for result in results}),
        "generated_at": utc_now(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "corpus": {"total_fixtures": len(fixtures), "by_speech_style": {style: sum(f["speech_style"] == style for f in fixtures) for style in sorted({f["speech_style"] for f in fixtures})}, "by_condition": {condition: sum(f["condition"] == condition for f in fixtures) for condition in sorted({f["condition"] for f in fixtures})}},
        "results": {"total": aggregate["total"], "succeeded": aggregate["succeeded"], "failed": aggregate["failed"], "by_mode": aggregate["by_mode"]},
        "recognition": {"micro_wer": aggregate["micro_wer"], "macro_wer": aggregate["macro_wer"], "critical_entity_exactness": aggregate["critical_entity_exactness"]},
        "shadow_correction": {"proposed_count": sum(r["shadow_correction"]["decision"] == "would_correct" for r in results), "clarification_count": sum(r["shadow_correction"]["decision"] == "would_clarify" for r in results), "wer_improvements": sum(r["metrics"]["shadow_wer"] < r["metrics"]["wer"] for r in results), "wer_regressions": sum(r["metrics"]["shadow_wer"] > r["metrics"]["wer"] for r in results), "critical_regressions": sum(r["metrics"]["shadow_critical_regression"] for r in results)},
        "latency": {"by_mode": aggregate["by_mode"]},
        "evidence_gaps": ["synthetic fixtures do not establish real Chennai speaker accuracy", "no native-speaker MOS or preference evidence", "provider pricing and retention terms not included"],
        "foundation_gates": [{"name": "all fixtures validated", "passed": True}, {"name": "no critical correction regression", "passed": not any(r["metrics"]["shadow_critical_regression"] for r in results)}, {"name": "all expected modes have result rows", "passed": coverage_complete}],
        "promotion_status": "evidence_only",
    }
    validate_report(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"Wrote report to {output_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--data-root", required=True)
    run = sub.add_parser("run-saaras")
    run.add_argument("--manifest", required=True); run.add_argument("--data-root", required=True); run.add_argument("--modes", default="transcribe,codemix"); run.add_argument("--run-id", required=True); run.add_argument("--output", required=True)
    report = sub.add_parser("report")
    report.add_argument("--manifest", required=True); report.add_argument("--data-root", required=True); report.add_argument("--results", required=True); report.add_argument("--report-id", required=True); report.add_argument("--output", required=True); report.add_argument("--expected-modes", default="transcribe,codemix")
    serve = sub.add_parser("serve")
    serve.add_argument("--data-root", required=True); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=3010); serve.add_argument("--collection-mode", default="disabled"); serve.add_argument("--trusted-host")
    args = parser.parse_args()
    if args.command == "validate":
        fixtures = validate_manifest(Path(args.manifest), Path(args.data_root)); print(f"Validated {len(fixtures)} fixtures"); return 0
    if args.command == "run-saaras": return asyncio.run(run_saaras(args))
    if args.command == "report": return build_report(args)
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            if args.host != "0.0.0.0" or not args.trusted_host:
                raise ValueError("non-loopback bind requires --host 0.0.0.0 and --trusted-host")
        if args.trusted_host and not __import__("re").fullmatch(r"[A-Za-z0-9.-]{1,253}", args.trusted_host):
            raise ValueError("invalid trusted host")
        import secrets
        import uvicorn
        from voice_eval.server import create_app
        token = secrets.token_urlsafe(32)
        data_root = Path(args.data_root)
        data_root.mkdir(parents=True, exist_ok=True)
        display_host = args.trusted_host or args.host
        print(f"Open http://{display_host}:{args.port}/#token={token}")
        if args.collection_mode == "founder_recording" and not args.trusted_host:
            print(f"Remote browser: ssh -N -L {args.port}:127.0.0.1:{args.port} xhdctallapa40")
            print(f"Then open http://127.0.0.1:{args.port}/#token={token}")
        uvicorn.run(create_app(data_root, args.collection_mode, token, args.trusted_host), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
