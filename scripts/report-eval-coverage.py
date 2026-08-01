#!/usr/bin/env python3
"""Report Fonely evaluation coverage by readiness profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CASES_DIR = PROJECT_ROOT / "evals" / "cases"

DOMAIN_MINIMUMS = {
    "pending_action": 25,
    "inventory": 35,
    "appointment": 40,
    "authorization": 25,
    "multilingual": 35,
    "medical_safety": 15,
    "voice_runtime": 10,
    "provider_routing": 5,
}
CHENNAI_LOCALE_MINIMUMS = {"ta-IN": 20, "en-IN": 20}
ALL_INDIA_LOCALE_MINIMUMS = {
    **CHENNAI_LOCALE_MINIMUMS,
    "hi-IN": 20,
    "te-IN": 15,
    "kn-IN": 15,
    "ml-IN": 15,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("structure", "chennai-pilot", "all-india"),
        default="chennai-pilot",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    return parser.parse_args()


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    if not CASES_DIR.exists():
        return cases
    for path in sorted(CASES_DIR.glob("*.jsonl")):
        with open(path) as file_obj:
            for line_number, raw_line in enumerate(file_obj, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{path.name}:{line_number}: malformed JSON: {exc}")
                    continue
                if not isinstance(value, dict):
                    errors.append(f"{path.name}:{line_number}: record must be an object")
                    continue
                cases.append(value)
    if errors:
        print("ERROR: Cannot report coverage with malformed corpus data.", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        sys.exit(1)
    return cases


def case_fingerprint(case: dict[str, Any]) -> tuple[str, str]:
    normalized = {
        "domain": case.get("domain"),
        "utterances": [
            turn.get("utterance", "").casefold().strip() for turn in case.get("turns", [])
        ],
        "intents": [turn.get("expected_intent") for turn in case.get("turns", [])],
        "tools": [turn.get("expected_tool") for turn in case.get("turns", [])],
        "arguments": [turn.get("expected_arguments") for turn in case.get("turns", [])],
        "forbidden": [turn.get("forbidden_behaviors", []) for turn in case.get("turns", [])],
    }
    text = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest(), text


def missing_counts(actual: Counter[str], requirements: dict[str, int]) -> dict[str, dict[str, int]]:
    return {
        key: {"required": minimum, "actual": actual.get(key, 0)}
        for key, minimum in requirements.items()
        if actual.get(key, 0) < minimum
    }


def analyze(cases: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    by_domain: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    by_risk: Counter[str] = Counter()
    by_locale: Counter[str] = Counter()
    by_caller_role: Counter[str] = Counter()
    by_tool: Counter[str] = Counter()
    by_language_review: Counter[str] = Counter()
    by_domain_review: Counter[str] = Counter()
    by_pilot: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    fingerprint_ids: defaultdict[str, list[str]] = defaultdict(list)
    fingerprint_texts: list[tuple[str, str]] = []

    critical_count = 0
    cases_with_forbidden = 0
    total_turns = 0
    turns_with_assertion = 0

    for case in cases:
        by_domain[case.get("domain", "unknown")] += 1
        by_category[case.get("category", "unknown")] += 1
        risk = case.get("risk_level", "unknown")
        by_risk[risk] += 1
        critical_count += risk == "critical"
        by_locale[case.get("locale", "unknown")] += 1
        by_caller_role[case.get("caller_role", "unknown")] += 1
        by_language_review[case.get("language_review_status", "unknown")] += 1
        by_domain_review[case.get("domain_review_status", "unknown")] += 1
        by_pilot[case.get("pilot_validation_status", "unknown")] += 1
        tag_counts.update(case.get("verification_tags", []))

        fingerprint, text = case_fingerprint(case)
        fingerprint_ids[fingerprint].append(case.get("case_id", "unknown"))
        fingerprint_texts.append((case.get("case_id", "unknown"), text))

        case_has_forbidden = False
        for turn in case.get("turns", []):
            total_turns += 1
            tool = turn.get("expected_tool")
            if tool:
                by_tool[tool] += 1
            case_has_forbidden |= bool(turn.get("forbidden_behaviors"))
            if any(
                (
                    turn.get("expected_response_constraints"),
                    turn.get("forbidden_behaviors"),
                    tool is not None,
                    turn.get("expected_outcome") is not None,
                    turn.get("expected_error_code") is not None,
                    turn.get("expected_database_effect") is not None,
                )
            ):
                turns_with_assertion += 1
        cases_with_forbidden += case_has_forbidden

    domain_gaps = missing_counts(by_domain, DOMAIN_MINIMUMS)
    chennai_gaps = missing_counts(by_locale, CHENNAI_LOCALE_MINIMUMS)
    all_india_gaps = missing_counts(by_locale, ALL_INDIA_LOCALE_MINIMUMS)

    if profile == "structure":
        blocking_failures = {"domain": domain_gaps}
    elif profile == "chennai-pilot":
        blocking_failures = {"domain": domain_gaps, "locale": chennai_gaps}
    else:
        blocking_failures = {"domain": domain_gaps, "locale": all_india_gaps}
    blocking_failures = {key: value for key, value in blocking_failures.items() if value}

    future_gaps = {
        locale: gap
        for locale, gap in all_india_gaps.items()
        if locale not in CHENNAI_LOCALE_MINIMUMS
    }

    exact_duplicates = [ids for ids in fingerprint_ids.values() if len(ids) > 1]
    near_duplicates: list[dict[str, Any]] = []
    for index, (case_id, text) in enumerate(fingerprint_texts):
        for other_id, other_text in fingerprint_texts[index + 1 :]:
            ratio = SequenceMatcher(None, text, other_text).ratio()
            if ratio >= 0.92:
                near_duplicates.append(
                    {"case_ids": [case_id, other_id], "similarity": round(ratio, 3)}
                )
                if len(near_duplicates) == 20:
                    break
        if len(near_duplicates) == 20:
            break

    total = len(cases)
    return {
        "profile": profile,
        "total_cases": total,
        "total_turns": total_turns,
        "turns_with_verifiable_assertion": turns_with_assertion,
        "by_domain": dict(by_domain.most_common()),
        "by_category": dict(by_category.most_common(30)),
        "by_risk_level": dict(by_risk.most_common()),
        "by_locale": dict(by_locale.most_common()),
        "by_caller_role": dict(by_caller_role.most_common()),
        "by_expected_tool": dict(by_tool.most_common()),
        "by_language_review_status": dict(by_language_review.most_common()),
        "by_domain_review_status": dict(by_domain_review.most_common()),
        "by_pilot_validation_status": dict(by_pilot.most_common()),
        "verification_tag_counts": dict(tag_counts.most_common(50)),
        "critical_case_count": critical_count,
        "critical_case_percentage": round(critical_count / total * 100, 1) if total else 0,
        "cases_with_forbidden_behaviors": cases_with_forbidden,
        "forbidden_behaviors_percentage": (
            round(cases_with_forbidden / total * 100, 1) if total else 0
        ),
        "blocking_failures": blocking_failures,
        "future_gaps": future_gaps,
        "exact_duplicate_fingerprints": exact_duplicates,
        "near_duplicate_candidates": near_duplicates,
    }


def print_mapping(title: str, values: dict[str, Any], width: int = 28) -> None:
    print(f"\n--- {title} ---")
    for key, value in values.items():
        print(f"  {key:{width}s} {value}")


def print_text(report: dict[str, Any]) -> None:
    print("=" * 64)
    print(f"Fonely Evaluation Coverage — profile: {report['profile']}")
    print("=" * 64)
    print(f"\nTotal cases: {report['total_cases']}")
    print(f"Total turns: {report['total_turns']}")
    print(
        "Turns with verifiable assertion: "
        f"{report['turns_with_verifiable_assertion']}/{report['total_turns']}"
    )
    print_mapping("By Domain", report["by_domain"])
    print_mapping("By Locale", report["by_locale"])
    print_mapping("By Risk Level", report["by_risk_level"])
    print_mapping("By Expected Tool", report["by_expected_tool"])
    print_mapping("Language Review", report["by_language_review_status"])
    print_mapping("Domain Review", report["by_domain_review_status"])
    print_mapping("Pilot Validation", report["by_pilot_validation_status"])

    if report["blocking_failures"]:
        print("\n--- BLOCKING COVERAGE FAILURES ---")
        for dimension, gaps in report["blocking_failures"].items():
            for key, gap in gaps.items():
                print(f"  {dimension}.{key}: have {gap['actual']}, need {gap['required']}")
    else:
        print("\nBlocking coverage thresholds met.")

    if report["future_gaps"]:
        print("\n--- FUTURE ALL-INDIA TARGETS (non-blocking for Chennai profile) ---")
        for locale, gap in report["future_gaps"].items():
            print(f"  {locale}: have {gap['actual']}, future target {gap['required']}")

    print(
        "\nSimilarity review: "
        f"{len(report['exact_duplicate_fingerprints'])} exact fingerprint group(s), "
        f"{len(report['near_duplicate_candidates'])} near-duplicate candidate(s)."
    )


def main() -> int:
    args = parse_args()
    cases = load_cases()
    if not cases:
        print("ERROR: No cases found.", file=sys.stderr)
        return 1
    report = analyze(cases, args.profile)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text(report)
    return 1 if report["blocking_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
