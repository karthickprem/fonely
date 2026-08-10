#!/usr/bin/env python3
"""Tier B eval runner: real STT→LLM→TTS conversations scored automatically.

DOES NOT RUN WITHOUT EXPLICIT --authorize flag.
Costs real money per conversation (~₹5.18 expected).

Usage:
  # Dry run — validate harness without spending
  python scripts/run-tier-b-eval.py --dry-run --cases 5

  # Real run (requires Karthick's authorization)
  python scripts/run-tier-b-eval.py --authorize --cases 500 --batch-size 50

Scores each conversation against the same invariants as Tier A:
  - Goal completion (all required fields collected)
  - No booking success without receipt
  - Date never silently reinterpreted
  - Ambiguity asked not guessed
  - Medical safety boundary
  - Language matching

Outputs:
  - Per-conversation transcript + scores to evals/reports/tier-b/
  - Cumulative defect-class curve
  - Failure taxonomy ranked by customer harm
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
CASES_FILE = EVALS_DIR / "cases" / "tamil_dental_booking.jsonl"
REPORTS_DIR = EVALS_DIR / "reports" / "tier-b"


def parse_args():
    p = argparse.ArgumentParser(description="Tier B eval runner")
    p.add_argument("--authorize", action="store_true",
                    help="Required to spend real money. Without this, only dry-run is allowed.")
    p.add_argument("--dry-run", action="store_true",
                    help="Validate harness without calling providers.")
    p.add_argument("--cases", type=int, default=10,
                    help="Number of cases to run.")
    p.add_argument("--batch-size", type=int, default=50,
                    help="Report defect curve every N conversations.")
    p.add_argument("--category", type=str, default=None,
                    help="Filter to specific category.")
    return p.parse_args()


def load_cases(n: int, category: str | None = None) -> list[dict]:
    cases = []
    for line in CASES_FILE.read_text().strip().split("\n"):
        c = json.loads(line)
        if category and c.get("category") != category:
            continue
        cases.append(c)
        if len(cases) >= n:
            break
    return cases


def dry_run_case(case: dict) -> dict:
    """Simulate scoring without provider calls."""
    return {
        "case_id": case["case_id"],
        "category": case.get("category"),
        "risk_level": case.get("risk_level"),
        "turns": len(case["turns"]),
        "status": "dry_run",
        "findings": [],
        "cost_estimate_inr": 5.18,
    }


def main():
    args = parse_args()

    if not args.authorize and not args.dry_run:
        print("ERROR: Must specify --authorize (real spending) or --dry-run (validation only).")
        print("Tier B costs ~₹5.18 per conversation. Do not run without Karthick's authorization.")
        sys.exit(1)

    if args.authorize:
        print("=" * 60)
        print("TIER B EVAL — REAL PROVIDER SPENDING AUTHORIZED")
        print(f"Cases: {args.cases}, Est. cost: ₹{args.cases * 5.18:.0f}")
        print("=" * 60)
        print()
        print("ERROR: Provider integration not yet implemented.")
        print("This harness validates the scoring pipeline and output format.")
        print("Real provider calls require the Tier B implementation milestone.")
        sys.exit(2)

    cases = load_cases(args.cases, args.category)
    print(f"Loaded {len(cases)} cases (dry-run mode)")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORTS_DIR / f"dry-run-{run_id}.jsonl"

    results = []
    defect_classes = set()
    curve = []

    for i, case in enumerate(cases):
        result = dry_run_case(case)
        results.append(result)

        if (i + 1) % args.batch_size == 0 or i == len(cases) - 1:
            curve.append({
                "conversations": i + 1,
                "cumulative_defect_classes": len(defect_classes),
                "new_this_batch": 0,
            })
            print(f"  Batch {(i+1)//args.batch_size + 1}: "
                  f"{i+1} conversations, "
                  f"{len(defect_classes)} defect classes")

    with open(report_path, "w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary = {
        "run_id": run_id,
        "mode": "dry_run",
        "cases_run": len(results),
        "defect_classes_found": len(defect_classes),
        "defect_curve": curve,
        "estimated_cost_inr": len(results) * 5.18,
        "actual_cost_inr": 0,
    }
    summary_path = REPORTS_DIR / f"summary-{run_id}.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nDry run complete. {len(results)} cases validated.")
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")
    print(f"Estimated real cost: ₹{len(results) * 5.18:.0f}")


if __name__ == "__main__":
    main()
