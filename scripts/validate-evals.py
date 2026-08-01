#!/usr/bin/env python3
"""Validate Fonely evaluation cases, tool contracts, and cross-field semantics."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print(
        "ERROR: jsonschema is required but is not installed. "
        "Dev1 must declare jsonschema>=4.26,<5 in backend QA/dev dependencies "
        "and regenerate uv.lock.",
        file=sys.stderr,
    )
    sys.exit(2)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SCHEMA_PATH = PROJECT_ROOT / "evals" / "schema" / "eval-case.schema.json"
TOOL_CONTRACT_SCHEMA_PATH = PROJECT_ROOT / "evals" / "schema" / "tool-contract.schema.json"
TOOL_CONTRACT_PATH = PROJECT_ROOT / "evals" / "tool-contract.v1.json"
INTENT_CONTRACT_SCHEMA_PATH = PROJECT_ROOT / "evals" / "schema" / "intent-contract.schema.json"
INTENT_CONTRACT_PATH = PROJECT_ROOT / "evals" / "intent-contract.v1.json"
CASES_DIR = PROJECT_ROOT / "evals" / "cases"

ERROR_OUTCOMES = frozenset(
    {
        "unauthorized",
        "insufficient_stock",
        "slot_conflict",
        "expired",
        "stale_version",
        "not_found",
        "validation_error",
        "provider_error",
    }
)
NON_ERROR_OUTCOMES = frozenset(
    {
        "success",
        "no_tool",
        "escalate",
        "clarification_needed",
        "information_presented",
        "authorization_denied",
        "validation_rejected",
        "runtime_recovered",
        "provider_recovered",
    }
)
E164_PATTERN = re.compile(r"\+[1-9]\d{6,14}")
FIXTURE_PHONE_PATTERN = re.compile(r"^\+919900\d{6}$")
DECIMAL_STRING_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.\d{1,2})?$")
DECIMAL_FIELDS = frozenset({"quantity", "new_price", "price", "amount", "total_amount"})
ID_FIELDS = frozenset(
    {
        "business_id",
        "action_id",
        "pending_action_id",
        "product_id",
        "service_id",
        "resource_id",
        "order_id",
        "appointment_id",
        "reservation_id",
        "entity_id",
    }
)
CREDENTIAL_PATTERNS = (
    re.compile(r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"gh[ps]_[a-zA-Z0-9]{36}"),
    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"postgresql(\+asyncpg)?://[^:]+:[^@]+@"),
)
EFFECTS_BY_WRITE_POLICY = {
    "none": frozenset({"no_write", "read_only", "escalation_recorded"}),
    "pending_only": frozenset(
        {
            "pending_action_created",
            "pending_action_revised",
            "pending_action_cancelled",
            "pending_action_expired",
            "temporary_hold_created",
            "temporary_hold_released",
        }
    ),
    "commit": frozenset(
        {
            "order_confirmed",
            "order_cancelled",
            "order_completed",
            "appointment_confirmed",
            "appointment_cancelled",
            "appointment_rescheduled",
            "stock_update_confirmed",
            "price_update_confirmed",
            "schedule_update_confirmed",
        }
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool-mismatch-report",
        type=Path,
        help="Write a machine-readable mismatch report to this path.",
    )
    return parser.parse_args()


def json_path(parts: list[Any]) -> str:
    if not parts:
        return "(root)"
    return ".".join(str(part) for part in parts)


def collect_phones(obj: object) -> list[str]:
    if isinstance(obj, str):
        return E164_PATTERN.findall(obj)
    if isinstance(obj, dict):
        return [phone for value in obj.values() for phone in collect_phones(value)]
    if isinstance(obj, list):
        return [phone for item in obj for phone in collect_phones(item)]
    return []


def find_credential(text: str) -> str | None:
    for pattern in CREDENTIAL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group()
    return None


def has_verifiable_assertion(turn: dict[str, Any]) -> bool:
    return any(
        (
            turn.get("expected_response_constraints"),
            turn.get("forbidden_behaviors"),
            turn.get("expected_tool") is not None,
            turn.get("expected_database_effect") is not None,
            turn.get("expected_outcome") is not None,
            turn.get("expected_error_code") is not None,
        )
    )


def iter_structured_fields(
    obj: object,
    path: tuple[Any, ...] = (),
    parent: dict[str, Any] | None = None,
) -> list[tuple[tuple[Any, ...], str, object, dict[str, Any]]]:
    fields: list[tuple[tuple[Any, ...], str, object, dict[str, Any]]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            current = (*path, key)
            fields.append((current, key, value, obj))
            fields.extend(iter_structured_fields(value, current, obj))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            fields.extend(iter_structured_fields(value, (*path, index), parent))
    return fields


class CorpusValidator:
    def __init__(self, mismatch_report_path: Path | None) -> None:
        self.errors: list[str] = []
        self.mismatches: list[dict[str, Any]] = []
        self.seen_ids: dict[str, str] = {}
        self.mismatch_report_path = mismatch_report_path
        self.case_validator: Draft202012Validator | None = None
        self.public_tools: dict[str, dict[str, Any]] = {}
        self.internal_operations: set[str] = set()
        self.argument_validators: dict[str, Draft202012Validator] = {}
        self.valid_outcomes: set[str] = set()
        self.valid_write_policies: set[str] = set()
        self.valid_intents: set[str] = set()
        self.intent_aliases: set[str] = set()
        self.case_count = 0
        self.turn_count = 0

    def error(
        self,
        file: str,
        line: int,
        message: str,
        *,
        case_id: str | None = None,
        turn_index: int | None = None,
        category: str = "validation",
        tool: str | None = None,
        path: list[Any] | None = None,
        validator: str | None = None,
    ) -> None:
        location = f"{file}:{line}"
        if turn_index is not None:
            location += f" turn[{turn_index}]"
        if path:
            location += f" {json_path(path)}"
        self.errors.append(f"{location}: {message}")
        self.mismatches.append(
            {
                "file": file,
                "line": line,
                "case_id": case_id,
                "turn_index": turn_index,
                "category": category,
                "tool": tool,
                "path": path or [],
                "validator": validator,
                "message": message,
            }
        )

    @staticmethod
    def load_json(path: Path) -> dict[str, Any]:
        with open(path) as file_obj:
            value = json.load(file_obj)
        if not isinstance(value, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return value

    def load_contracts(self) -> bool:
        required = (
            SCHEMA_PATH,
            TOOL_CONTRACT_SCHEMA_PATH,
            TOOL_CONTRACT_PATH,
            INTENT_CONTRACT_SCHEMA_PATH,
            INTENT_CONTRACT_PATH,
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            for path in missing:
                print(f"ERROR: Required file not found: {path}", file=sys.stderr)
            return False

        try:
            eval_schema = self.load_json(SCHEMA_PATH)
            contract_schema = self.load_json(TOOL_CONTRACT_SCHEMA_PATH)
            contract = self.load_json(TOOL_CONTRACT_PATH)
            intent_schema = self.load_json(INTENT_CONTRACT_SCHEMA_PATH)
            intent_contract = self.load_json(INTENT_CONTRACT_PATH)
            Draft202012Validator.check_schema(eval_schema)
            Draft202012Validator.check_schema(contract_schema)
            Draft202012Validator.check_schema(intent_schema)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR: Invalid schema/contract JSON: {exc}", file=sys.stderr)
            return False

        contract_validator = Draft202012Validator(contract_schema)
        contract_errors = sorted(
            contract_validator.iter_errors(contract), key=lambda error: list(error.absolute_path)
        )
        if contract_errors:
            print("ERROR: Tool contract does not match tool-contract.schema.json:", file=sys.stderr)
            for error in contract_errors:
                print(
                    f"  {TOOL_CONTRACT_PATH.name}:{json_path(list(error.absolute_path))}: "
                    f"{error.message}",
                    file=sys.stderr,
                )
            return False

        intent_errors = sorted(
            Draft202012Validator(intent_schema).iter_errors(intent_contract),
            key=lambda error: list(error.absolute_path),
        )
        if intent_errors:
            print(
                "ERROR: Intent contract does not match intent-contract.schema.json:",
                file=sys.stderr,
            )
            for error in intent_errors:
                print(
                    f"  {INTENT_CONTRACT_PATH.name}:{json_path(list(error.absolute_path))}: "
                    f"{error.message}",
                    file=sys.stderr,
                )
            return False

        outcomes = contract["outcomes"]
        write_policies = contract["write_policies"]
        public_tools = contract["public_tools"]

        semantic_errors: list[str] = []
        for tool_name, tool in public_tools.items():
            try:
                Draft202012Validator.check_schema(tool["arguments"])
            except Exception as exc:  # jsonschema exposes multiple schema-error subclasses
                semantic_errors.append(f"{tool_name}.arguments is not a valid JSON Schema: {exc}")
            unknown_outcomes = set(tool["allowed_outcomes"]) - set(outcomes)
            unknown_writes = set(tool["allowed_write_policies"]) - set(write_policies)
            if unknown_outcomes:
                semantic_errors.append(
                    f"{tool_name}.allowed_outcomes has unknown values: {sorted(unknown_outcomes)}"
                )
            if unknown_writes:
                values = sorted(unknown_writes)
                semantic_errors.append(
                    f"{tool_name}.allowed_write_policies has unknown values: {values}"
                )

        if semantic_errors:
            print("ERROR: Tool contract semantic validation failed:", file=sys.stderr)
            for message in semantic_errors:
                print(f"  {message}", file=sys.stderr)
            return False

        self.case_validator = Draft202012Validator(eval_schema)
        self.public_tools = public_tools
        self.internal_operations = set(contract["internal_operations"])
        shared_defs = contract.get("$defs", {})
        self.argument_validators = {
            name: Draft202012Validator({**tool["arguments"], "$defs": shared_defs})
            for name, tool in public_tools.items()
        }
        self.valid_outcomes = set(outcomes)
        self.valid_write_policies = set(write_policies)
        self.valid_intents = set(intent_contract["intents"])
        self.intent_aliases = set(intent_contract["aliases"])
        return True

    def validate_tool_policy(
        self,
        turn: dict[str, Any],
        file: str,
        line: int,
        case_id: str,
        turn_index: int,
    ) -> None:
        policy = turn.get("expected_tool_policy")
        tool = turn.get("expected_tool")
        arguments = turn.get("expected_arguments")

        if tool in self.internal_operations:
            self.error(
                file,
                line,
                f"internal operation '{tool}' is not LLM-callable",
                case_id=case_id,
                turn_index=turn_index,
                category="internal_tool",
                tool=tool,
            )
            return

        if policy == "required":
            if tool is None or arguments is None:
                self.error(
                    file,
                    line,
                    "required tool policy needs non-null expected_tool and expected_arguments",
                    case_id=case_id,
                    turn_index=turn_index,
                    category="tool_policy",
                    tool=tool,
                )
        elif policy == "forbidden":
            if tool is not None or arguments is not None:
                self.error(
                    file,
                    line,
                    "forbidden tool policy requires null expected_tool and expected_arguments",
                    case_id=case_id,
                    turn_index=turn_index,
                    category="tool_policy",
                    tool=tool,
                )
            return
        elif policy == "optional" and (tool is None) != (arguments is None):
            self.error(
                file,
                line,
                "optional tool policy requires tool and arguments to be both null or both non-null",
                case_id=case_id,
                turn_index=turn_index,
                category="tool_policy",
                tool=tool,
            )

        if tool is None:
            return
        if tool not in self.public_tools:
            self.error(
                file,
                line,
                f"expected_tool '{tool}' is not a public tool in the contract",
                case_id=case_id,
                turn_index=turn_index,
                category="unknown_tool",
                tool=tool,
            )
            return
        if arguments is None:
            return

        argument_errors = sorted(
            self.argument_validators[tool].iter_errors(arguments),
            key=lambda error: list(error.absolute_path),
        )
        for error in argument_errors:
            self.error(
                file,
                line,
                error.message,
                case_id=case_id,
                turn_index=turn_index,
                category="tool_arguments",
                tool=tool,
                path=list(error.absolute_path),
                validator=error.validator,
            )

    def validate_outcome_and_write(
        self,
        turn: dict[str, Any],
        file: str,
        line: int,
        case_id: str,
        turn_index: int,
    ) -> None:
        tool = turn.get("expected_tool")
        tool_policy = turn.get("expected_tool_policy")
        outcome = turn.get("expected_outcome")
        error_code = turn.get("expected_error_code")
        write_policy = turn.get("expected_write_policy")
        effect = turn.get("expected_database_effect")

        if turn.get("speaker") == "caller" and outcome is None:
            self.error(
                file,
                line,
                "caller turn requires a non-null expected_outcome",
                case_id=case_id,
                turn_index=turn_index,
                category="caller_outcome",
                tool=tool,
            )

        if outcome in NON_ERROR_OUTCOMES and error_code is not None:
            self.error(
                file,
                line,
                f"non-error outcome '{outcome}' requires null expected_error_code",
                case_id=case_id,
                turn_index=turn_index,
                category="outcome_error",
                tool=tool,
            )
        if outcome in ERROR_OUTCOMES and error_code != outcome:
            self.error(
                file,
                line,
                f"error outcome '{outcome}' requires expected_error_code '{outcome}'",
                case_id=case_id,
                turn_index=turn_index,
                category="outcome_error",
                tool=tool,
            )
        if error_code is not None and error_code not in ERROR_OUTCOMES:
            self.error(
                file,
                line,
                f"unknown expected_error_code '{error_code}'",
                case_id=case_id,
                turn_index=turn_index,
                category="outcome_error",
                tool=tool,
            )
        if outcome == "no_tool" and tool_policy != "forbidden":
            self.error(
                file,
                line,
                "expected_outcome 'no_tool' requires expected_tool_policy 'forbidden'",
                case_id=case_id,
                turn_index=turn_index,
                category="outcome_tool_policy",
                tool=tool,
            )

        if tool in self.public_tools:
            contract = self.public_tools[tool]
            if outcome is not None and outcome not in contract["allowed_outcomes"]:
                self.error(
                    file,
                    line,
                    f"tool '{tool}' does not allow outcome '{outcome}'",
                    case_id=case_id,
                    turn_index=turn_index,
                    category="tool_outcome",
                    tool=tool,
                )
            if write_policy not in contract["allowed_write_policies"]:
                self.error(
                    file,
                    line,
                    f"tool '{tool}' does not allow write policy '{write_policy}'",
                    case_id=case_id,
                    turn_index=turn_index,
                    category="tool_write_policy",
                    tool=tool,
                )

        if effect is None:
            return
        operation = effect.get("operation") if isinstance(effect, dict) else None
        if operation not in EFFECTS_BY_WRITE_POLICY.get(write_policy, frozenset()):
            self.error(
                file,
                line,
                f"database effect operation '{operation}' contradicts "
                f"write policy '{write_policy}'",
                case_id=case_id,
                turn_index=turn_index,
                category="write_effect",
                tool=tool,
                path=["expected_database_effect", "operation"],
            )

    def validate_intent(
        self,
        turn: dict[str, Any],
        file: str,
        line: int,
        case_id: str,
        turn_index: int,
    ) -> None:
        intent = turn.get("expected_intent")
        if intent is None:
            return
        if intent in self.intent_aliases:
            self.error(
                file,
                line,
                f"intent alias '{intent}' is not valid in cases; use its canonical label",
                case_id=case_id,
                turn_index=turn_index,
                category="intent_alias",
                path=["expected_intent"],
            )
        elif intent not in self.valid_intents:
            self.error(
                file,
                line,
                f"unknown expected_intent '{intent}'",
                case_id=case_id,
                turn_index=turn_index,
                category="unknown_intent",
                path=["expected_intent"],
            )

    def validate_structured_values(
        self,
        obj: object,
        file: str,
        line: int,
        case_id: str | None,
        path_prefix: tuple[Any, ...] = (),
    ) -> None:
        for path, field, value, parent in iter_structured_fields(obj, path_prefix):
            if (
                field in ID_FIELDS
                and value is not None
                and (isinstance(value, bool) or not isinstance(value, int) or value <= 0)
            ):
                self.error(
                    file,
                    line,
                    f"structured ID '{field}' must be a positive integer or null",
                    case_id=case_id,
                    category="identifier",
                    path=list(path),
                )
            if (
                field in DECIMAL_FIELDS
                and value is not None
                and (not isinstance(value, str) or not DECIMAL_STRING_PATTERN.fullmatch(value))
            ):
                self.error(
                    file,
                    line,
                    f"structured decimal '{field}' must be a numeric string "
                    "with at most two decimals",
                    case_id=case_id,
                    category="decimal",
                    path=list(path),
                )
                continue
            if field == "quantity":
                unit = parent.get("unit")
                if not isinstance(unit, str) or not unit:
                    self.error(
                        file,
                        line,
                        "structured quantity requires an explicit unit",
                        case_id=case_id,
                        category="unit",
                        path=list((*path[:-1], "unit")),
                    )

    def validate_tool_selectors(
        self,
        turn: dict[str, Any],
        file: str,
        line: int,
        case_id: str,
        turn_index: int,
    ) -> None:
        tool = turn.get("expected_tool")
        arguments = turn.get("expected_arguments")
        if not isinstance(arguments, dict):
            return
        selector_pairs = {
            "check_inventory": (("product_id", "product_name"), ("product_ids", "product_names")),
            "create_pending_order": (),
            "propose_stock_update": (),
            "propose_price_update": (("product_id", "product_name"),),
            "check_availability": (),
            "create_pending_appointment": (("service_id", "service_name"),),
        }
        if tool not in selector_pairs:
            return
        pairs = selector_pairs[tool]
        if tool == "check_inventory" and arguments.get("list_all"):
            return
        if pairs and not any(
            arguments.get(left) is not None or arguments.get(right) is not None
            for left, right in pairs
        ):
            self.error(
                file,
                line,
                f"tool '{tool}' requires an ID or name selector",
                case_id=case_id,
                turn_index=turn_index,
                category="tool_selector",
                tool=tool,
            )

    def validate_record(self, record: dict[str, Any], file: str, line: int) -> None:
        assert self.case_validator is not None
        case_id = record.get("case_id") if isinstance(record.get("case_id"), str) else None

        for error in sorted(
            self.case_validator.iter_errors(record), key=lambda item: list(item.absolute_path)
        ):
            self.error(
                file,
                line,
                f"schema: {error.message}",
                case_id=case_id,
                category="case_schema",
                path=list(error.absolute_path),
                validator=error.validator,
            )

        self.validate_structured_values(
            record.get("existing_state"), file, line, case_id, ("existing_state",)
        )

        if case_id:
            if case_id in self.seen_ids:
                self.error(
                    file,
                    line,
                    f"duplicate case_id '{case_id}' (first: {self.seen_ids[case_id]})",
                    case_id=case_id,
                    category="duplicate_case_id",
                )
            else:
                self.seen_ids[case_id] = f"{file}:{line}"

        for turn_index, turn in enumerate(record.get("turns", [])):
            if not isinstance(turn, dict) or case_id is None:
                continue
            self.turn_count += 1
            self.validate_tool_policy(turn, file, line, case_id, turn_index)
            self.validate_tool_selectors(turn, file, line, case_id, turn_index)
            self.validate_outcome_and_write(turn, file, line, case_id, turn_index)
            self.validate_intent(turn, file, line, case_id, turn_index)
            self.validate_structured_values(
                turn.get("expected_arguments"),
                file,
                line,
                case_id,
                ("turns", turn_index, "expected_arguments"),
            )
            self.validate_structured_values(
                turn.get("expected_database_effect"),
                file,
                line,
                case_id,
                ("turns", turn_index, "expected_database_effect"),
            )
            if not has_verifiable_assertion(turn):
                self.error(
                    file,
                    line,
                    "turn has no verifiable assertion",
                    case_id=case_id,
                    turn_index=turn_index,
                    category="empty_assertion",
                    tool=turn.get("expected_tool"),
                )

        raw = json.dumps(record, ensure_ascii=False)
        credential = find_credential(raw)
        if credential:
            self.error(
                file,
                line,
                f"possible credential pattern: '{credential}'",
                case_id=case_id,
                category="credential",
            )
        for phone in collect_phones(record):
            if not FIXTURE_PHONE_PATTERN.fullmatch(phone):
                self.error(
                    file,
                    line,
                    f"non-fixture phone number: '{phone}'",
                    case_id=case_id,
                    category="phone",
                )

    def validate_file(self, path: Path) -> int:
        count = 0
        with open(path) as file_obj:
            for line_number, raw_line in enumerate(file_obj, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    self.error(
                        path.name,
                        line_number,
                        f"malformed JSON: {exc}",
                        category="malformed_json",
                    )
                    continue
                if not isinstance(record, dict):
                    self.error(
                        path.name,
                        line_number,
                        "record must be a JSON object",
                        category="case_schema",
                    )
                    continue
                self.validate_record(record, path.name, line_number)
                count += 1
        self.case_count += count
        return count

    def write_mismatch_report(self) -> None:
        if self.mismatch_report_path is None:
            return
        report_path = self.mismatch_report_path
        if not report_path.is_absolute():
            report_path = PROJECT_ROOT / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)

        failing_turns = {
            (item["case_id"], item["turn_index"])
            for item in self.mismatches
            if item["case_id"] is not None and item["turn_index"] is not None
        }
        by_category = Counter(item["category"] for item in self.mismatches)
        by_tool = Counter(item["tool"] for item in self.mismatches if item["tool"])
        by_validator = Counter(item["validator"] for item in self.mismatches if item["validator"])
        payload = {
            "report_version": 1,
            "phase": "qa2_validation",
            "total_cases": self.case_count,
            "total_turns": self.turn_count,
            "failing_turns": len(failing_turns),
            "individual_mismatches": len(self.mismatches),
            "by_category": dict(sorted(by_category.items())),
            "by_tool": dict(sorted(by_tool.items())),
            "by_validator": dict(sorted(by_validator.items())),
            "mismatches": self.mismatches,
        }
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def run(self) -> int:
        if not self.load_contracts():
            return 1
        if not CASES_DIR.exists():
            print(f"ERROR: Cases directory not found at {CASES_DIR}", file=sys.stderr)
            return 1

        files = sorted(CASES_DIR.glob("*.jsonl"))
        if not files:
            print(f"ERROR: No JSONL files found in {CASES_DIR}", file=sys.stderr)
            return 1

        for path in files:
            print(f"  {path.name}: {self.validate_file(path)} cases")
        print(f"\nTotal cases: {self.case_count}")
        print(f"Total turns: {self.turn_count}")
        self.write_mismatch_report()

        if self.errors:
            print(f"\n{len(self.errors)} error(s):\n", file=sys.stderr)
            for message in self.errors:
                print(f"  {message}", file=sys.stderr)
            return 1
        print("\nAll cases valid.")
        return 0


def main() -> int:
    args = parse_args()
    return CorpusValidator(args.tool_mismatch_report).run()


if __name__ == "__main__":
    sys.exit(main())
