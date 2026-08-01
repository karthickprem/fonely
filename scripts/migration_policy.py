"""Static source and rendered-SQL policy checks for migration extensions."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOWED_EXTENSIONS = frozenset({"btree_gist"})
EXTENSION_KEYWORD = re.compile(r"\b(?:CREATE|DROP)\s+EXTENSION\b", re.IGNORECASE)
CREATE_EXTENSION = re.compile(
    r"^\s*CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?P<name>"[A-Za-z_][A-Za-z0-9_]*"|[A-Za-z_][A-Za-z0-9_]*)\s*;?\s*$',
    re.IGNORECASE,
)
DROP_EXTENSION = re.compile(r"^\s*DROP\s+EXTENSION\b", re.IGNORECASE)
EXTENSION_FRAGMENT = re.compile(
    r"\b(?:CREATE|DROP)\s+EXTENSION\b[^;]*(?:;|$)", re.IGNORECASE
)
RENDERED_CREATE_EXTENSION = re.compile(
    r"\bCREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r'(?P<name>"[A-Za-z_][A-Za-z0-9_]*"|[A-Za-z_][A-Za-z0-9_]*)\s*(?=;|$)',
    re.IGNORECASE,
)
RENDERED_DROP_EXTENSION = re.compile(r"\bDROP\s+EXTENSION\b", re.IGNORECASE)
RENDERED_GIST_EXCLUSION = re.compile(r"\bEXCLUDE\s+USING\s+gist\b", re.IGNORECASE)


@dataclass(frozen=True)
class PolicyResult:
    findings: tuple[str, ...]
    errors: tuple[str, ...]
    requested_extensions: tuple[str, ...] = ()


def _is_op_execute(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "execute"
        and isinstance(func.value, ast.Name)
        and func.value.id == "op"
    )


def _normalize_identifier(raw_name: str) -> str:
    name = raw_name[1:-1] if raw_name.startswith('"') else raw_name
    return name.lower()


def scan_source(path: Path) -> PolicyResult:
    """Check literal extension requests without inferring rendered DDL order."""
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    basename = path.name
    errors: list[str] = []
    findings: list[str] = []
    requested: list[str] = []
    recognized_constants: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_op_execute(node) or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            sql = argument.value
            recognized_constants.add(id(argument))
            if not EXTENSION_KEYWORD.search(sql):
                continue
            fragments = EXTENSION_FRAGMENT.findall(sql)
            if not fragments:
                errors.append(
                    f"{basename}:{node.lineno}: extension SQL could not be parsed"
                )
                continue
            for fragment in fragments:
                statement = fragment.strip()
                if DROP_EXTENSION.match(statement):
                    errors.append(
                        f"{basename}:{node.lineno}: DROP EXTENSION is forbidden"
                    )
                    continue
                match = CREATE_EXTENSION.fullmatch(statement)
                if match is None:
                    errors.append(
                        f"{basename}:{node.lineno}: unsupported CREATE EXTENSION statement"
                    )
                    continue
                name = _normalize_identifier(match.group("name"))
                if name not in ALLOWED_EXTENSIONS:
                    errors.append(
                        f"{basename}:{node.lineno}: CREATE EXTENSION '{name}' is not allowlisted"
                    )
                    continue
                requested.append(name)
                findings.append(
                    f"{basename}:{node.lineno}: CREATE EXTENSION '{name}' is allowlisted"
                )
            continue

        fragments = " ".join(
            child.value
            for child in ast.walk(argument)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
        segment = ast.get_source_segment(source, argument) or ""
        if EXTENSION_KEYWORD.search(fragments) or re.search(
            r"extension", segment, re.IGNORECASE
        ):
            errors.append(
                f"{basename}:{node.lineno}: dynamic or non-literal extension SQL is forbidden"
            )

    # Extension text in variables, aliases, helpers, or other calls must not pass silently.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and EXTENSION_KEYWORD.search(node.value)
            and id(node) not in recognized_constants
        ):
            errors.append(
                f"{basename}:{node.lineno}: extension SQL must be a literal op.execute argument"
            )

    return PolicyResult(
        findings=tuple(findings),
        errors=tuple(dict.fromkeys(errors)),
        requested_extensions=tuple(requested),
    )


def _strip_sql_comments_and_strings(sql: str) -> str:
    """Blank comments and single-quoted values while preserving positions and identifiers."""
    chars = list(sql)
    index = 0
    state = "code"
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current == "-" and following == "-":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if current == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if current == "'":
                chars[index] = " "
                index += 1
                state = "string"
                continue
        elif state == "line_comment":
            if current == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block_comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "code"
                continue
            if current != "\n":
                chars[index] = " "
        elif state == "string":
            if current == "'" and following == "'":
                chars[index] = chars[index + 1] = " "
                index += 2
                continue
            if current == "'":
                chars[index] = " "
                state = "code"
            elif current != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def scan_rendered_sql(
    *,
    upgrade_sql: str,
    downgrade_sql: str,
    requested_extensions: tuple[str, ...],
    migration_name: str,
    revision: str,
) -> PolicyResult:
    """Verify extension behavior in one rendered revision upgrade/downgrade pair."""
    label = f"{migration_name} revision {revision}"
    normalized_upgrade = _strip_sql_comments_and_strings(upgrade_sql)
    normalized_downgrade = _strip_sql_comments_and_strings(downgrade_sql)
    errors: list[str] = []
    findings: list[str] = []

    creates = [
        (_normalize_identifier(match.group("name")), match.start())
        for match in RENDERED_CREATE_EXTENSION.finditer(normalized_upgrade)
    ]
    rendered_extension_fragments = EXTENSION_FRAGMENT.findall(normalized_upgrade)
    if len(creates) != len(rendered_extension_fragments):
        errors.append(f"{label}: rendered upgrade contains unsupported extension SQL")
    for name, _ in creates:
        if name not in ALLOWED_EXTENSIONS:
            errors.append(f"{label}: rendered unknown extension '{name}'")

    for requested in dict.fromkeys(requested_extensions):
        positions = [position for name, position in creates if name == requested]
        if not positions:
            errors.append(f"{label}: rendered upgrade does not create '{requested}'")
            continue
        if requested == "btree_gist":
            exclusions = [
                match.start()
                for match in RENDERED_GIST_EXCLUSION.finditer(normalized_upgrade)
            ]
            if not exclusions:
                errors.append(
                    f"{label}: rendered upgrade has no EXCLUDE USING gist after btree_gist"
                )
            elif min(positions) > min(exclusions):
                errors.append(
                    f"{label}: rendered btree_gist creation must precede EXCLUDE USING gist"
                )
            else:
                findings.append(
                    f"{label}: rendered btree_gist creation precedes EXCLUDE USING gist"
                )

    if RENDERED_DROP_EXTENSION.search(normalized_downgrade):
        errors.append(f"{label}: rendered downgrade must not DROP EXTENSION")

    return PolicyResult(findings=tuple(findings), errors=tuple(errors))


def _emit(result: PolicyResult) -> int:
    for finding in result.findings:
        print(f"INFO: {finding}")
    for requested in result.requested_extensions:
        print(f"REQUESTED_EXTENSION={requested}")
    for error in result.errors:
        print(f"ERROR: {error}")
    return 1 if result.errors else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    source_parser = subparsers.add_parser("source")
    source_parser.add_argument("migration", type=Path)
    rendered_parser = subparsers.add_parser("rendered")
    rendered_parser.add_argument("--upgrade-sql", required=True, type=Path)
    rendered_parser.add_argument("--downgrade-sql", required=True, type=Path)
    rendered_parser.add_argument("--migration-name", required=True)
    rendered_parser.add_argument("--revision", required=True)
    rendered_parser.add_argument("--requested-extension", action="append", default=[])
    args = parser.parse_args()

    if args.command == "source":
        return _emit(scan_source(args.migration))
    return _emit(
        scan_rendered_sql(
            upgrade_sql=args.upgrade_sql.read_text(),
            downgrade_sql=args.downgrade_sql.read_text(),
            requested_extensions=tuple(args.requested_extension),
            migration_name=args.migration_name,
            revision=args.revision,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
