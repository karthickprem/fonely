"""Fail-closed source, graph, rendering, and SQL policy for Alembic migrations."""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import itertools
import json
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

PROTOCOL_VERSION = 1
ALLOWED_EXTENSION = "btree_gist"
REVISION_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
RESERVED = frozenset({"base", "head", "heads", "current"})
METADATA_NAMES = ("revision", "down_revision", "branch_labels", "depends_on")
SINK_METHODS = frozenset({"execute", "exec_driver_sql"})
EXTENSION_OBJECT_PREFIXES = (
    ("COMMENT", "ON", "EXTENSION"),
    ("SECURITY", "LABEL", "ON", "EXTENSION"),
)
EXTENSION_OBJECT_INFIXES = (
    ("GRANT", "ON", "EXTENSION"),
    ("REVOKE", "ON", "EXTENSION"),
)


class PolicyError(Exception):
    """A sanitized expected policy failure."""


@dataclass(frozen=True)
class SourceExtension:
    direction: str
    line: int
    name: str


@dataclass(frozen=True)
class SourceInfo:
    revision: str
    parents: tuple[str, ...]
    branch_labels: tuple[str, ...]
    dependencies: tuple[str, ...]
    path: Path
    merge: bool
    extensions: tuple[SourceExtension, ...]


@dataclass(frozen=True)
class RevisionInfo:
    revision: str
    parents: tuple[str, ...]
    dependencies: tuple[str, ...]
    children: tuple[str, ...]
    branch_labels: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class GraphInfo:
    revisions: tuple[RevisionInfo, ...]
    heads: tuple[str, ...]
    bases: tuple[str, ...]


@dataclass(frozen=True)
class SqlToken:
    kind: str
    value: str
    position: int


@dataclass(frozen=True)
class Identifier:
    value: str
    quoted: bool


@dataclass(frozen=True)
class RelationKey:
    schema: Identifier | None
    relation: Identifier


@dataclass(frozen=True)
class ConstraintKey:
    relation: RelationKey
    constraint: Identifier


@dataclass(frozen=True)
class ExtensionOperation:
    operation: str
    name: str | None
    position: int


@dataclass(frozen=True)
class SqlEvidence:
    extensions: tuple[ExtensionOperation, ...]
    exclusions: tuple[tuple[ConstraintKey, int], ...]
    dropped_exclusions: tuple[tuple[ConstraintKey, int], ...]
    dropped_relations: tuple[tuple[RelationKey, int], ...]
    renamed_relations: tuple[tuple[RelationKey, int], ...]


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    sql: str


def _assignment_map(tree: ast.Module, filename: str) -> dict[str, ast.AST]:
    assignments: dict[str, ast.AST] = {}
    for node in tree.body:
        target: str | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign):
            names = [item.id for item in node.targets if isinstance(item, ast.Name)]
            if len(names) == 1:
                target, value = names[0], node.value
        if target in METADATA_NAMES:
            if target in assignments or value is None:
                raise PolicyError(f"{filename}: invalid metadata declaration")
            assignments[target] = value
    missing = [name for name in METADATA_NAMES if name not in assignments]
    if missing:
        raise PolicyError(f"{filename}: missing explicit migration metadata")
    return assignments


def _literal(node: ast.AST, filename: str) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise PolicyError(f"{filename}: migration metadata must be literal") from exc


def _safe_revision(value: Any, filename: str, field: str) -> str:
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value):
        raise PolicyError(f"{filename}: invalid {field} identifier")
    if value.lower() in RESERVED:
        raise PolicyError(f"{filename}: reserved {field} identifier")
    return value


def _references(value: Any, filename: str, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values: tuple[Any, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (tuple, list)):
        values = tuple(value)
    else:
        raise PolicyError(f"{filename}: invalid {field} metadata type")
    references = tuple(_safe_revision(item, filename, field) for item in values)
    if len(references) != len(set(references)):
        raise PolicyError(f"{filename}: duplicate {field} edge")
    return references


def _labels(value: Any, filename: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list, set, frozenset)):
        raise PolicyError(f"{filename}: invalid branch_labels metadata type")
    labels: list[str] = []
    for label in value:
        if (
            not isinstance(label, str)
            or not LABEL_RE.fullmatch(label)
            or label.lower() in RESERVED
        ):
            raise PolicyError(f"{filename}: invalid branch label")
        labels.append(label)
    if len(labels) != len(set(labels)):
        raise PolicyError(f"{filename}: duplicate branch label")
    return tuple(sorted(labels))


def _meaningful(body: list[ast.stmt]) -> bool:
    return any(
        not isinstance(node, ast.Pass)
        and not (
            isinstance(node, ast.Expr)
            and isinstance(
                node.value,
                (
                    ast.Constant,
                    ast.List,
                    ast.Tuple,
                    ast.Set,
                    ast.Dict,
                ),
            )
        )
        for node in body
    )


def _call_name(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_call_name(node.value), node.attr)
    if isinstance(node, ast.Call) and _call_name(node.func) == ("op", "get_bind"):
        return ("op", "get_bind()")
    return ()


def _execution_sink(
    call: ast.Call, aliases: dict[str, tuple[str, ...]]
) -> tuple[str, ...] | None:
    name = _call_name(call.func)
    if len(name) == 1 and name[0] in aliases:
        name = aliases[name[0]]
    if (
        len(name) == 2
        and name[1] in SINK_METHODS
        and aliases.get(f"__binding__:{name[0]}") == ("binding",)
    ):
        return ("binding", name[1])
    allowed = {
        ("op", "execute"),
        ("op", "get_bind()", "execute"),
        ("op", "get_bind()", "exec_driver_sql"),
        ("connection", "execute"),
        ("connection", "exec_driver_sql"),
        ("bind", "execute"),
        ("bind", "exec_driver_sql"),
        ("binding", "execute"),
        ("binding", "exec_driver_sql"),
    }
    return name if name in allowed else None


def _sql_argument(call: ast.Call, filename: str, sink: tuple[str, ...]) -> ast.AST:
    supported = {"sqltext"}
    unknown = [keyword.arg for keyword in call.keywords if keyword.arg not in supported]
    maximum_positional = 1 if sink == ("op", "execute") else 2
    if (
        unknown
        or (not call.args and not call.keywords)
        or len(call.args) > maximum_positional
    ):
        raise PolicyError(f"{filename}: malformed database execution call")
    keyword_values = [
        keyword.value for keyword in call.keywords if keyword.arg == "sqltext"
    ]
    if (1 if call.args else 0) + len(keyword_values) != 1:
        raise PolicyError(f"{filename}: malformed database execution call")
    return call.args[0] if call.args else keyword_values[0]


def _static_sql(node: ast.AST, constants: dict[str, str]) -> tuple[str | None, bool]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id], False
    if isinstance(node, (ast.JoinedStr, ast.FormattedValue)):
        fragments = " ".join(
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
        normalized = re.sub(r"(?i)extension", "", fragments)
        if fragments and normalized != fragments:
            return None, True
        return None, False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_dynamic = _static_sql(node.left, constants)
        right, right_dynamic = _static_sql(node.right, constants)
        if left is not None and right is not None:
            return left + right, False
        return None, left_dynamic or right_dynamic or bool(
            re.search(r"(?i)extension", f"{left or ''} {right or ''}")
        )
    if (
        isinstance(node, ast.Call)
        and _call_name(node.func) in {("sa", "text"), ("sqlalchemy", "text")}
        and len(node.args) == 1
        and not node.keywords
    ):
        return _static_sql(node.args[0], constants)
    fragments = " ".join(
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )
    extension_sensitive = bool(re.search(r"(?i)extension", fragments))
    return None, extension_sensitive


def _aliases_and_constants(
    function: ast.FunctionDef,
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    aliases: dict[str, tuple[str, ...]] = {}
    constants: dict[str, str] = {}
    bindings: set[str] = set()
    assignments: list[tuple[ast.Name, ast.AST]] = []
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            assignments.append((node.targets[0], node.value))
        elif isinstance(node, ast.AnnAssign):
            if node.value is None:
                if not isinstance(node.target, ast.Name):
                    raise PolicyError("unsupported annotated assignment target")
                continue
            if not isinstance(node.target, ast.Name):
                raise PolicyError("unsupported annotated assignment target")
            assignments.append((node.target, node.value))
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            value_name = _call_name(value)
            if (
                (
                    value_name == ("op", "get_bind()")
                    and isinstance(value, ast.Call)
                )
                or (isinstance(value, ast.Name) and value.id in bindings)
            ):
                if target.id not in bindings:
                    bindings.add(target.id)
                    changed = True
            elif (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in bindings
                and value.attr in SINK_METHODS
            ):
                resolved = ("binding", value.attr)
                if aliases.get(target.id) != resolved:
                    aliases[target.id] = resolved
                    changed = True
            elif isinstance(value, ast.Name) and value.id in aliases:
                resolved = aliases[value.id]
                if aliases.get(target.id) != resolved:
                    aliases[target.id] = resolved
                    changed = True
            elif value_name in {
                ("op", "execute"),
                ("op", "get_bind()", "execute"),
                ("op", "get_bind()", "exec_driver_sql"),
            }:
                if aliases.get(target.id) != value_name:
                    aliases[target.id] = value_name
                    changed = True
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                constants[target.id] = value.value
    for binding in bindings:
        aliases[f"__binding__:{binding}"] = ("binding",)
    return aliases, constants


def _function_map(tree: ast.Module, filename: str) -> dict[str, ast.FunctionDef]:
    functions: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in functions or isinstance(node, ast.AsyncFunctionDef):
                raise PolicyError(f"{filename}: invalid migration function declaration")
            functions[node.name] = node
    if "upgrade" not in functions or "downgrade" not in functions:
        raise PolicyError(f"{filename}: missing upgrade or downgrade function")
    return functions


def _bind_helper_call(function: ast.FunctionDef, call: ast.Call) -> dict[str, ast.AST]:
    parameter_names = [argument.arg for argument in function.args.args]
    if function.args.vararg or function.args.kwarg:
        raise PolicyError("unsupported helper call binding")
    if any(keyword.arg is None for keyword in call.keywords) or any(
        isinstance(argument, ast.Starred) for argument in call.args
    ):
        raise PolicyError("unsupported helper call binding")
    keyword_map = {keyword.arg: keyword.value for keyword in call.keywords}
    if len(keyword_map) != len(call.keywords):
        raise PolicyError("unsupported helper call binding")
    if any(name not in parameter_names for name in keyword_map):
        raise PolicyError("unsupported helper call binding")
    if len(call.args) > len(parameter_names):
        raise PolicyError("unsupported helper call binding")
    bound = {
        parameter_names[index]: argument for index, argument in enumerate(call.args)
    }
    if any(name in bound for name in keyword_map):
        raise PolicyError("unsupported helper call binding")
    bound.update(keyword_map)
    if set(bound) != set(parameter_names):
        raise PolicyError("unsupported helper call binding")
    return bound


def _resolve_parameter_values(
    tree: ast.Module,
    functions: dict[str, ast.FunctionDef],
    function: ast.FunctionDef,
    parameter: str,
    visited: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[str, ...]:
    key = (function.name, parameter)
    if key in visited:
        raise PolicyError("recursive helper forwarding is forbidden")
    parameter_names = {argument.arg for argument in function.args.args}
    if parameter not in parameter_names:
        raise PolicyError("unresolved database execution")
    callers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func) == (function.name,)
    ]
    if not callers:
        raise PolicyError("unresolved database execution")
    values: list[str] = []
    for call in callers:
        bound = _bind_helper_call(function, call)
        argument = bound[parameter]
        value, _ = _static_sql(argument, {})
        if value is not None:
            values.append(value)
            continue
        enclosing = next(
            (
                candidate
                for candidate in functions.values()
                if any(node is call for node in ast.walk(candidate))
            ),
            None,
        )
        if enclosing is None or not isinstance(argument, ast.Name):
            raise PolicyError("unresolved database execution")
        enclosing_parameters = {item.arg for item in enclosing.args.args}
        if argument.id in enclosing_parameters:
            forwarded_parameter = argument.id
        else:
            assigned_loop_source = next(
                (
                    node.iter
                    for node in ast.walk(enclosing)
                    if isinstance(node, (ast.For, ast.AsyncFor))
                    and isinstance(node.target, (ast.Tuple, ast.List))
                    and any(
                        isinstance(item, ast.Name) and item.id == argument.id
                        for item in node.target.elts
                    )
                ),
                None,
            )
            if isinstance(assigned_loop_source, ast.Name) and (
                assigned_loop_source.id in enclosing_parameters
            ):
                forwarded_parameter = assigned_loop_source.id
            elif isinstance(assigned_loop_source, ast.Name):
                assigned_value = next(
                    (
                        node.value
                        for node in enclosing.body
                        if isinstance(node, ast.Assign)
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == assigned_loop_source.id
                    ),
                    None,
                )
                if not isinstance(assigned_value, (ast.Tuple, ast.List)):
                    raise PolicyError("unresolved database execution")
                assigned_loop_source = assigned_value
                static_values: list[str] = []
                target_index = next(
                    index
                    for index, item in enumerate(
                        next(
                            node.target
                            for node in ast.walk(enclosing)
                            if isinstance(node, (ast.For, ast.AsyncFor))
                            and isinstance(node.iter, ast.Name)
                            and node.iter.id
                            == next(
                                target.id
                                for node in enclosing.body
                                if isinstance(node, ast.Assign)
                                for target in node.targets
                                if isinstance(target, ast.Name)
                                and node.value is assigned_value
                            )
                        ).elts
                    )
                    if isinstance(item, ast.Name) and item.id == argument.id
                )
                for item in assigned_loop_source.elts:
                    if not isinstance(item, (ast.Tuple, ast.List)):
                        raise PolicyError("unresolved database execution")
                    value, _ = _static_sql(item.elts[target_index], {})
                    if value is None:
                        raise PolicyError("unresolved database execution")
                    static_values.append(value)
                values.extend(static_values)
                continue
            elif isinstance(assigned_loop_source, (ast.Tuple, ast.List)):
                static_values: list[str] = []
                target_index = next(
                    index
                    for index, item in enumerate(
                        next(
                            node.target
                            for node in ast.walk(enclosing)
                            if isinstance(node, (ast.For, ast.AsyncFor))
                            and node.iter is assigned_loop_source
                        ).elts
                    )
                    if isinstance(item, ast.Name) and item.id == argument.id
                )
                for item in assigned_loop_source.elts:
                    if not isinstance(
                        item, (ast.Tuple, ast.List)
                    ) or target_index >= len(item.elts):
                        raise PolicyError("unresolved database execution")
                    value, _ = _static_sql(item.elts[target_index], {})
                    if value is None:
                        raise PolicyError("unresolved database execution")
                    static_values.append(value)
                values.extend(static_values)
                continue
            else:
                raise PolicyError("unresolved database execution")
        values.extend(
            _resolve_parameter_values(
                tree,
                functions,
                enclosing,
                forwarded_parameter,
                visited | {key},
            )
        )
    return tuple(values)


def _static_parameter_values(
    tree: ast.Module,
    functions: dict[str, ast.FunctionDef],
    function: ast.FunctionDef,
    parameter: str,
) -> tuple[str, ...] | None:
    if parameter not in {argument.arg for argument in function.args.args}:
        return None
    return _resolve_parameter_values(tree, functions, function, parameter)


def _statement_level_extension(
    function: ast.FunctionDef, call: ast.Call, direction: str, filename: str
) -> bool:
    if direction != "upgrade":
        return False
    return any(
        isinstance(statement, ast.Expr) and statement.value is call
        for statement in function.body
    )


def source_info(path: Path) -> SourceInfo:
    filename = path.name
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise PolicyError(f"{filename}: source parse/read failure") from exc
    assignments = _assignment_map(tree, filename)
    revision = _safe_revision(
        _literal(assignments["revision"], filename), filename, "revision"
    )
    parents = _references(
        _literal(assignments["down_revision"], filename), filename, "down_revision"
    )
    labels = _labels(_literal(assignments["branch_labels"], filename), filename)
    dependencies = _references(
        _literal(assignments["depends_on"], filename), filename, "depends_on"
    )
    functions = _function_map(tree, filename)
    merge = len(parents) > 1
    if not merge and (
        not _meaningful(functions["upgrade"].body)
        or not _meaningful(functions["downgrade"].body)
    ):
        raise PolicyError(f"{filename}: ordinary migration body is empty")

    extensions: list[SourceExtension] = []
    database_calls: set[int] = set()
    owners: list[tuple[str, ast.FunctionDef]] = [
        ("upgrade", functions["upgrade"]),
        ("downgrade", functions["downgrade"]),
        *[
            ("helper", function)
            for name, function in functions.items()
            if name not in {"upgrade", "downgrade"}
        ],
    ]
    for direction, function in owners:
        aliases, constants = _aliases_and_constants(function)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            sink = _execution_sink(node, aliases)
            if sink is None:
                continue
            database_calls.add(id(node))
            argument = _sql_argument(node, filename, sink)
            sql, sensitive = _static_sql(argument, constants)
            sql_values: tuple[str, ...]
            parameter_name: str | None = None
            if isinstance(argument, ast.Name):
                parameter_name = argument.id
            elif (
                isinstance(argument, ast.Call)
                and _call_name(argument.func)
                in {("sa", "text"), ("sqlalchemy", "text")}
                and len(argument.args) == 1
                and isinstance(argument.args[0], ast.Name)
            ):
                parameter_name = argument.args[0].id
            if sql is None and parameter_name is not None:
                parameter_values = _static_parameter_values(
                    tree, functions, function, parameter_name
                )
                sql_values = parameter_values or ()
            else:
                sql_values = (sql,) if sql is not None else ()
            if not sql_values:
                if direction == "helper" and not sensitive:
                    parameter_names = {
                        parameter.arg for parameter in function.args.args
                    }
                    if parameter_name in parameter_names or isinstance(
                        argument, ast.JoinedStr
                    ):
                        continue
                category = (
                    "ambiguous extension-sensitive execution"
                    if sensitive
                    else "unresolved database execution"
                )
                raise PolicyError(f"{filename}:{node.lineno}: {category}")
            evidences = [inspect_sql(value, procedural=False) for value in sql_values]
            extension_evidence = [
                evidence for evidence in evidences if evidence.extensions
            ]
            if not extension_evidence:
                continue
            evidence = extension_evidence[0]
            if len(extension_evidence) != 1:
                raise PolicyError(
                    f"{filename}:{node.lineno}: unsupported extension source"
                )
            if (
                sink != ("op", "execute")
                or _call_name(node.func) != ("op", "execute")
                or not isinstance(argument, ast.Constant)
            ):
                raise PolicyError(
                    f"{filename}:{node.lineno}: extension source must be direct literal op.execute"
                )
            if not _statement_level_extension(function, node, direction, filename):
                raise PolicyError(
                    f"{filename}:{node.lineno}: extension execution has no direct upgrade owner"
                )
            if len(evidence.extensions) != 1:
                raise PolicyError(
                    f"{filename}:{node.lineno}: unsupported extension source"
                )
            operation = evidence.extensions[0]
            if operation.operation != "CREATE" or operation.name != ALLOWED_EXTENSION:
                raise PolicyError(
                    f"{filename}:{node.lineno}: forbidden extension source"
                )
            extensions.append(SourceExtension(direction, node.lineno, operation.name))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in database_calls:
            continue
        fragments = "".join(
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
        normalized_fragments = re.sub(r"(?s)/\*.*?\*/|\s+", "", fragments)
        if re.search(r"(?i)(?:create|drop|alter)extension", normalized_fragments):
            raise PolicyError(
                f"{filename}:{node.lineno}: unowned extension-sensitive call"
            )
    return SourceInfo(
        revision, parents, labels, dependencies, path, merge, tuple(extensions)
    )


def sql_tokens(sql: str) -> tuple[SqlToken, ...]:
    tokens: list[SqlToken] = []
    index = 0
    while index < len(sql):
        current = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if current.isspace():
            index += 1
            continue
        if current == "\\" and (
            index == 0 or not sql[sql.rfind("\n", 0, index) + 1 : index].strip()
        ):
            raise PolicyError("psql meta-command is forbidden")
        if current == "-" and following == "-":
            match = re.search(r"[\r\n]", sql[index + 2 :])
            index = len(sql) if match is None else index + 2 + match.end()
            continue
        if current == "/" and following == "*":
            index += 2
            depth = 1
            while index < len(sql) and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise PolicyError("unterminated block comment")
            continue
        escape = (
            current in "Ee"
            and following == "'"
            and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] in "_$"))
        )
        if current == "'" or escape:
            index += 2 if escape else 1
            closed = False
            while index < len(sql):
                if escape and sql[index] == "\\":
                    index += 2
                    continue
                if sql[index] == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                    index += 2
                    continue
                if sql[index] == "'":
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise PolicyError("unterminated string")
            continue
        if current == '"':
            start = index
            index += 1
            value: list[str] = []
            closed = False
            while index < len(sql):
                if sql[index : index + 2] == '""':
                    value.append('"')
                    index += 2
                elif sql[index] == '"':
                    index += 1
                    closed = True
                    break
                else:
                    value.append(sql[index])
                    index += 1
            if not closed:
                raise PolicyError("unterminated quoted identifier")
            tokens.append(SqlToken("identifier", "".join(value), start))
            continue
        if current == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
            if match:
                tag = match.group(0)
                body_start = index + len(tag)
                body_end = sql.find(tag, body_start)
                if body_end < 0:
                    raise PolicyError("unterminated dollar quote")
                tokens.append(SqlToken("dollar", sql[body_start:body_end], index))
                index = body_end + len(tag)
                continue
        if current.isalpha() or current == "_":
            start = index
            index += 1
            while index < len(sql) and (sql[index].isalnum() or sql[index] in "_$"):
                index += 1
            tokens.append(SqlToken("word", sql[start:index], start))
            continue
        tokens.append(SqlToken("symbol", current, index))
        index += 1
    return tuple(tokens)


def _word(token: SqlToken, value: str) -> bool:
    return token.kind == "word" and token.value.upper() == value


def _statements(tokens: tuple[SqlToken, ...]) -> tuple[tuple[SqlToken, ...], ...]:
    statements: list[tuple[SqlToken, ...]] = []
    current: list[SqlToken] = []
    for token in tokens:
        if token.value == ";":
            if current:
                statements.append(tuple(current))
                current = []
        else:
            current.append(token)
    if current:
        statements.append(tuple(current))
    return tuple(statements)


def _procedural_bodies(statement: tuple[SqlToken, ...]) -> tuple[str, ...]:
    words = [token.value.upper() for token in statement if token.kind == "word"]
    executable = (
        bool(words and words[0] == "DO")
        or words[:2]
        in (
            ["CREATE", "FUNCTION"],
            ["CREATE", "PROCEDURE"],
        )
        or words[:4]
        in (
            ["CREATE", "OR", "REPLACE", "FUNCTION"],
            ["CREATE", "OR", "REPLACE", "PROCEDURE"],
        )
    )
    if not executable:
        return ()
    bodies = tuple(token.value for token in statement if token.kind == "dollar")
    if not bodies:
        raise PolicyError("procedural SQL has no analyzable body")
    return bodies


def _extension_statement(statement: tuple[SqlToken, ...]) -> ExtensionOperation | None:
    if (
        len(statement) >= 2
        and statement[0].kind == "word"
        and _word(statement[1], "EXTENSION")
    ):
        operation = statement[0].value.upper()
        if operation not in {"CREATE", "ALTER", "DROP"}:
            return None
        if operation != "CREATE":
            return ExtensionOperation(operation, None, statement[0].position)
        remainder = list(statement[2:])
        cursor = 0
        if len(remainder) >= 3 and all(
            _word(remainder[offset], expected)
            for offset, expected in enumerate(("IF", "NOT", "EXISTS"))
        ):
            cursor = 3
        if cursor + 1 != len(remainder) or remainder[cursor].kind not in {
            "word",
            "identifier",
        }:
            return ExtensionOperation("UNSUPPORTED", None, statement[0].position)
        name_token = remainder[cursor]
        if (
            name_token.kind == "identifier"
            and name_token.value != name_token.value.lower()
        ):
            return ExtensionOperation("UNSUPPORTED", None, statement[0].position)
        return ExtensionOperation(
            "CREATE", name_token.value.lower(), statement[0].position
        )
    words = tuple(token.value.upper() for token in statement if token.kind == "word")
    if (
        any(words[: len(prefix)] == prefix for prefix in EXTENSION_OBJECT_PREFIXES)
        or any(
            len(words) >= 3
            and words[0] == prefix[0]
            and "ON" in words
            and "EXTENSION" in words
            for prefix in EXTENSION_OBJECT_INFIXES
        )
        or (
            words
            and words[0] == "ALTER"
            and "DEPENDS" in words
            and "EXTENSION" in words
        )
    ):
        return ExtensionOperation("OBJECT", None, statement[0].position)
    if "EXTENSION" in words:
        raise PolicyError("ambiguous extension syntax")
    return None


def _identifier(token: SqlToken) -> Identifier:
    if token.kind == "word":
        return Identifier(token.value.lower(), False)
    if token.kind == "identifier":
        return Identifier(token.value, True)
    raise PolicyError("ambiguous SQL identifier")


def _relation_name(statement: tuple[SqlToken, ...]) -> RelationKey:
    if len(statement) < 3 or not _word(statement[1], "TABLE"):
        raise PolicyError("ambiguous relation-qualified constraint")
    index = 2
    if (
        index + 1 < len(statement)
        and _word(statement[index], "IF")
        and _word(statement[index + 1], "EXISTS")
    ):
        index += 2
        if index < len(statement) and _word(statement[index], "ONLY"):
            index += 1
    elif index < len(statement) and _word(statement[index], "ONLY"):
        index += 1
    if index >= len(statement) or statement[index].kind not in {"word", "identifier"}:
        raise PolicyError("ambiguous relation-qualified constraint")
    if statement[index].kind == "word" and statement[index].value.upper() in {
        "IF",
        "EXISTS",
        "ONLY",
        "ADD",
        "DROP",
        "RENAME",
        "ALTER",
    }:
        raise PolicyError("ambiguous relation-qualified constraint")
    first = _identifier(statement[index])
    if index + 1 < len(statement) and statement[index + 1].value == ".":
        if index + 2 >= len(statement):
            raise PolicyError("ambiguous relation-qualified constraint")
        return RelationKey(first, _identifier(statement[index + 2]))
    return RelationKey(None, first)


def _exclusion_create(
    statement: tuple[SqlToken, ...],
) -> tuple[ConstraintKey, int] | None:
    if not statement or not _word(statement[0], "ALTER"):
        return None
    words = [token.value.upper() for token in statement if token.kind == "word"]
    if (
        len(words) < 7
        or words[1] != "TABLE"
        or "ADD" not in words
        or "CONSTRAINT" not in words
    ):
        return None
    for index in range(len(statement) - 2):
        if (
            _word(statement[index], "EXCLUDE")
            and _word(statement[index + 1], "USING")
            and _word(statement[index + 2], "GIST")
        ):
            constraint_index = next(
                (
                    offset
                    for offset, token in enumerate(statement)
                    if _word(token, "CONSTRAINT")
                ),
                -1,
            )
            if constraint_index < 0 or constraint_index + 1 >= len(statement):
                raise PolicyError("ambiguous exclusion constraint")
            name = statement[constraint_index + 1]
            if name.kind not in {"word", "identifier"}:
                raise PolicyError("ambiguous exclusion constraint")
            key = ConstraintKey(_relation_name(statement), _identifier(name))
            return key, statement[index].position
    return None


def _exclusion_drop(
    statement: tuple[SqlToken, ...],
) -> tuple[ConstraintKey, int] | None:
    words = [token.value.upper() for token in statement if token.kind == "word"]
    if (
        len(words) < 5
        or words[:2] != ["ALTER", "TABLE"]
        or "DROP" not in words
        or "CONSTRAINT" not in words
    ):
        return None
    index = next(
        (
            offset
            for offset, token in enumerate(statement)
            if _word(token, "CONSTRAINT")
        ),
        -1,
    )
    if index < 0 or index + 1 >= len(statement):
        raise PolicyError("ambiguous constraint drop")
    name_index = index + 1
    if (
        name_index + 1 < len(statement)
        and _word(statement[name_index], "IF")
        and _word(statement[name_index + 1], "EXISTS")
    ):
        name_index += 2
    if name_index >= len(statement) or statement[name_index].kind not in {
        "word",
        "identifier",
    }:
        raise PolicyError("ambiguous constraint drop")
    key = ConstraintKey(_relation_name(statement), _identifier(statement[name_index]))
    return key, statement[index].position


def _dropped_relation(
    statement: tuple[SqlToken, ...],
) -> tuple[RelationKey, int] | None:
    if (
        len(statement) < 3
        or not _word(statement[0], "DROP")
        or not _word(statement[1], "TABLE")
    ):
        return None
    index = 2
    if (
        index + 1 < len(statement)
        and _word(statement[index], "IF")
        and _word(statement[index + 1], "EXISTS")
    ):
        index += 2
    if index >= len(statement) or statement[index].kind not in {"word", "identifier"}:
        raise PolicyError("ambiguous table drop")
    first = _identifier(statement[index])
    if index + 2 < len(statement) and statement[index + 1].value == ".":
        relation = RelationKey(first, _identifier(statement[index + 2]))
    else:
        relation = RelationKey(None, first)
    return relation, statement[0].position


def _renamed_relation(
    statement: tuple[SqlToken, ...],
) -> tuple[RelationKey, int] | None:
    words = [token.value.upper() for token in statement if token.kind == "word"]
    if words[:2] != ["ALTER", "TABLE"] or "RENAME" not in words:
        return None
    return _relation_name(statement), statement[0].position


def _has_inline_exclusion(statement: tuple[SqlToken, ...]) -> bool:
    words = [token.value.upper() for token in statement if token.kind == "word"]
    if words[:2] != ["CREATE", "TABLE"]:
        return False
    return any(
        _word(statement[index], "EXCLUDE")
        and index + 2 < len(statement)
        and _word(statement[index + 1], "USING")
        and _word(statement[index + 2], "GIST")
        for index in range(len(statement))
    )


def inspect_sql(sql: str, *, procedural: bool = True) -> SqlEvidence:
    tokens = sql_tokens(sql)
    operations: list[ExtensionOperation] = []
    exclusions: list[tuple[ConstraintKey, int]] = []
    drops: list[tuple[ConstraintKey, int]] = []
    dropped_relations: list[tuple[RelationKey, int]] = []
    renamed_relations: list[tuple[RelationKey, int]] = []
    for statement in _statements(tokens):
        words = [token.value.upper() for token in statement if token.kind == "word"]
        if (
            "STANDARD_CONFORMING_STRINGS" in words
            and words[0] in {"SET", "RESET"}
            and not (words[0] == "SET" and words[-1] in {"ON", "TRUE", "1"})
        ):
            raise PolicyError("unsupported string-conformance mode")
        operation = _extension_statement(statement)
        if operation:
            operations.append(operation)
        if _has_inline_exclusion(statement):
            raise PolicyError("unsupported inline exclusion syntax")
        created = _exclusion_create(statement)
        if created:
            exclusions.append(created)
        dropped = _exclusion_drop(statement)
        if dropped:
            drops.append(dropped)
        dropped_relation = _dropped_relation(statement)
        if dropped_relation:
            dropped_relations.append(dropped_relation)
        renamed_relation = _renamed_relation(statement)
        if renamed_relation:
            renamed_relations.append(renamed_relation)
        if procedural:
            for body in _procedural_bodies(statement):
                string_fragments = "".join(
                    fragment.replace("''", "'")
                    for fragment in re.findall(r"'((?:''|[^'])*)'", body)
                )
                extension_sensitive = bool(
                    re.search(r"(?i)\bextension\b", body)
                    or re.search(r"(?i)\bextension\b", string_fragments)
                )
                reviewed_lock = re.fullmatch(
                    r"(?is)\s*DECLARE\s+table_name\s+text\s*;\s*BEGIN\s+"
                    r"FOREACH\s+table_name\s+IN\s+ARRAY\s+ARRAY\s*\[.*?\]\s+LOOP\s+"
                    r"IF\s+to_regclass\s*\(\s*format\s*\(\s*'%I\.%I'\s*,\s*"
                    r"current_schema\s*\(\s*\)\s*,\s*table_name\s*\)\s*\)\s+"
                    r"IS\s+NOT\s+NULL\s+THEN\s+EXECUTE\s+format\s*\(\s*"
                    r"'LOCK TABLE %I\.%I IN SHARE ROW EXCLUSIVE MODE'\s*,\s*"
                    r"current_schema\s*\(\s*\)\s*,\s*table_name\s*\)\s*;\s*"
                    r"END\s+IF\s*;\s*END\s+LOOP\s*;\s*END\s*",
                    body,
                )
                executable_body = re.sub(r"'(?:''|[^'])*'", " ", body)
                unsupported_execution = re.search(
                    r"(?i)\b(?:EXECUTE|PERFORM|CALL)\b", executable_body
                )
                perform_targets = re.findall(
                    r"(?i)\bPERFORM\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                    executable_body,
                )
                reviewed_internal_perform = bool(perform_targets) and {
                    target.lower() for target in perform_targets
                } == {"enforce_one_confirmed_appointment_allocation"}
                if reviewed_internal_perform:
                    residual = re.sub(
                        r"(?is)\bPERFORM\s+"
                        r"enforce_one_confirmed_appointment_allocation\s*\(.*?\)\s*;",
                        " ",
                        executable_body,
                    )
                    if re.search(r"(?i)\b(?:EXECUTE|PERFORM|CALL)\b", residual):
                        reviewed_internal_perform = False
                if (
                    unsupported_execution
                    and not reviewed_lock
                    and not reviewed_internal_perform
                ):
                    raise PolicyError("unsupported procedural SQL")
                if extension_sensitive:
                    raise PolicyError("procedural extension behavior is forbidden")
    return SqlEvidence(
        tuple(operations),
        tuple(exclusions),
        tuple(drops),
        tuple(dropped_relations),
        tuple(renamed_relations),
    )


def _candidate_files(versions_dir: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for path in versions_dir.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.py", path.name):
            raise PolicyError("migration filename is not safely reportable")
        if path.is_symlink():
            raise PolicyError(f"{path.name}: symlinked migration is forbidden")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(versions_dir):
            raise PolicyError(f"{path.name}: migration escapes versions directory")
        candidates.append(resolved)
    return tuple(sorted(candidates))


def discover_graph(
    config_path: Path, versions_dir: Path
) -> tuple[GraphInfo, dict[str, SourceInfo]]:
    candidates = _candidate_files(versions_dir)
    if not candidates:
        raise PolicyError("revision graph contains no migrations")
    sources = {
        info.revision: info for info in (source_info(path) for path in candidates)
    }
    if len(sources) != len(candidates):
        raise PolicyError("duplicate revision metadata")
    capture_out, capture_err = io.StringIO(), io.StringIO()
    try:
        with (
            contextlib.redirect_stdout(capture_out),
            contextlib.redirect_stderr(capture_err),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("error")
            config = Config(str(config_path))
            directory = ScriptDirectory.from_config(config)
            scripts = list(directory.walk_revisions(base="base", head="heads"))
            heads = tuple(sorted(directory.get_heads(consider_depends_on=True)))
            bases = tuple(sorted(directory.get_bases()))
    except Exception as exc:
        raise PolicyError("revision graph discovery/import failure") from exc
    if capture_out.getvalue() or capture_err.getvalue():
        raise PolicyError("revision graph import emitted output")
    if len(heads) != 1:
        raise PolicyError("revision graph must have exactly one effective head")
    by_revision = {script.revision: script for script in scripts}
    if len(by_revision) != len(scripts):
        raise PolicyError("duplicate Alembic revision")
    for revision, script in by_revision.items():
        original_labels = tuple(sorted(script._orig_branch_labels))
        if original_labels != sources[revision].branch_labels:
            raise PolicyError(
                f"{sources[revision].path.name}: imported branch-label metadata mismatch"
            )
    graph_paths = {Path(script.path).resolve(strict=True) for script in scripts}
    if graph_paths != set(candidates) or set(by_revision) != set(sources):
        raise PolicyError("filesystem and Alembic revision trees differ")

    children: dict[str, set[str]] = {revision: set() for revision in by_revision}
    parent_map: dict[str, tuple[str, ...]] = {}
    dependency_map: dict[str, tuple[str, ...]] = {}
    for revision, script in by_revision.items():
        source = sources[revision]
        raw_parents = script.down_revision
        parents = (
            ()
            if raw_parents is None
            else (raw_parents,)
            if isinstance(raw_parents, str)
            else tuple(raw_parents)
        )
        raw_dependencies = script.dependencies
        dependencies = (
            ()
            if raw_dependencies is None
            else (raw_dependencies,)
            if isinstance(raw_dependencies, str)
            else tuple(raw_dependencies)
        )
        if parents != source.parents or tuple(dependencies) != source.dependencies:
            raise PolicyError(f"{source.path.name}: graph metadata mismatch")
        for parent in (*parents, *dependencies):
            if parent not in by_revision:
                raise PolicyError(f"{source.path.name}: missing graph dependency")
            children[parent].add(revision)
        parent_map[revision] = parents
        dependency_map[revision] = tuple(dependencies)

    indegree = {
        revision: len({*parent_map[revision], *dependency_map[revision]})
        for revision in by_revision
    }
    ready = sorted(revision for revision, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        revision = ready.pop(0)
        order.append(revision)
        for child in sorted(children[revision]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(order) != len(by_revision):
        raise PolicyError("revision graph traversal is incomplete")
    revisions = tuple(
        RevisionInfo(
            revision,
            parent_map[revision],
            dependency_map[revision],
            tuple(sorted(children[revision])),
            sources[revision].branch_labels,
            sources[revision].path,
        )
        for revision in order
    )
    return GraphInfo(revisions, heads, bases), sources


def _render(
    alembic: Path,
    config_path: Path,
    backend_root: Path,
    direction: str,
    target: str,
    timeout: int,
) -> RenderResult:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "DATABASE_URL": os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://localhost:55432/fonely_test",
        ),
    }
    try:
        completed = subprocess.run(
            [str(alembic), "-c", str(config_path), direction, target, "--sql"],
            cwd=backend_root,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return RenderResult(False, "")
    if completed.returncode != 0:
        return RenderResult(False, "")
    if not completed.stdout.strip() or "Running " not in completed.stdout:
        return RenderResult(False, "")
    return RenderResult(True, completed.stdout)


def _apply_sql_policy(evidence: SqlEvidence, direction: str) -> None:
    for operation in evidence.extensions:
        if direction != "upgrade":
            raise PolicyError(
                f"cumulative {direction}: extension state mutation is forbidden"
            )
        if operation.operation != "CREATE" or operation.name != ALLOWED_EXTENSION:
            raise PolicyError("cumulative upgrade: forbidden extension operation")


def _ddl_counts(sql: str) -> dict[str, int]:
    tokens = sql_tokens(sql)
    words = [token.value.upper() for token in tokens if token.kind == "word"]
    pairs = list(itertools.pairwise(words))
    return {
        "create_table": pairs.count(("CREATE", "TABLE")),
        "drop_table": pairs.count(("DROP", "TABLE")),
        "add_column": pairs.count(("ADD", "COLUMN")),
        "drop_column": pairs.count(("DROP", "COLUMN")),
        "check_constraint": words.count("CHECK"),
        "create_index": pairs.count(("CREATE", "INDEX")),
        "drop_index": pairs.count(("DROP", "INDEX")),
    }


def check_repository(
    backend_root: Path,
    versions_dir: Path,
    config_path: Path,
    alembic: Path,
    timeout: int,
) -> dict[str, object]:
    graph, sources = discover_graph(config_path, versions_dir)
    head = graph.heads[0]
    upgrade = _render(
        alembic, config_path, backend_root, "upgrade", "base:heads", timeout
    )
    downgrade = _render(
        alembic, config_path, backend_root, "downgrade", "heads:base", timeout
    )
    if not upgrade.ok:
        raise PolicyError("cumulative upgrade render failure")
    if not downgrade.ok:
        raise PolicyError("cumulative downgrade render failure")
    upgrade_evidence = inspect_sql(upgrade.sql)
    downgrade_evidence = inspect_sql(downgrade.sql)
    _apply_sql_policy(upgrade_evidence, "upgrade")
    _apply_sql_policy(downgrade_evidence, "downgrade")

    source_extensions = [
        extension for source in sources.values() for extension in source.extensions
    ]
    rendered_extensions = [
        operation
        for operation in upgrade_evidence.extensions
        if operation.operation == "CREATE" and operation.name == ALLOWED_EXTENSION
    ]
    if len(source_extensions) != len(rendered_extensions):
        raise PolicyError("source/rendered extension ownership mismatch")
    if len(rendered_extensions) > 1:
        raise PolicyError("duplicate extension ownership is forbidden")

    active_exclusions: dict[ConstraintKey, int] = {}
    exclusion_events = (
        [(position, "create", name) for name, position in upgrade_evidence.exclusions]
        + [
            (position, "drop", name)
            for name, position in upgrade_evidence.dropped_exclusions
        ]
        + [
            (position, "drop_relation", relation)
            for relation, position in upgrade_evidence.dropped_relations
        ]
    )
    for position, operation, name in sorted(exclusion_events):
        if operation == "create":
            active_exclusions[name] = position
        elif operation == "drop":
            active_exclusions.pop(name, None)
        else:
            active_exclusions = {
                key: created
                for key, created in active_exclusions.items()
                if key.relation != name
            }
    for relation, _ in upgrade_evidence.renamed_relations:
        if any(key.relation == relation for key in active_exclusions):
            raise PolicyError("tracked exclusion relation rename is unsupported")
    if active_exclusions and len(rendered_extensions) != 1:
        raise PolicyError(
            "surviving GiST exclusion requires approved extension ownership"
        )
    if rendered_extensions:
        extension_position = rendered_extensions[0].position
        if not active_exclusions:
            raise PolicyError(
                "approved extension has no surviving later GiST exclusion"
            )
        if any(
            position < extension_position for position in active_exclusions.values()
        ):
            raise PolicyError(
                "surviving GiST exclusion precedes extension installation"
            )

    # Exact ranges are additional evidence only where dependency attribution is unambiguous.
    for revision in graph.revisions:
        source = sources[revision.revision]
        if len(revision.parents) <= 1 and not revision.dependencies:
            parent = revision.parents[0] if revision.parents else "base"
            exact_up = _render(
                alembic,
                config_path,
                backend_root,
                "upgrade",
                f"{parent}:{revision.revision}",
                timeout,
            )
            exact_down = _render(
                alembic,
                config_path,
                backend_root,
                "downgrade",
                f"{revision.revision}:{parent}",
                timeout,
            )
            if not exact_up.ok or not exact_down.ok:
                raise PolicyError(f"{source.path.name}: exact revision render failure")
            exact_up_evidence = inspect_sql(exact_up.sql)
            exact_down_evidence = inspect_sql(exact_down.sql)
            _apply_sql_policy(exact_up_evidence, "upgrade")
            _apply_sql_policy(exact_down_evidence, "downgrade")
            exact_creates = [
                operation
                for operation in exact_up_evidence.extensions
                if operation.operation == "CREATE"
                and operation.name == ALLOWED_EXTENSION
            ]
            if len(exact_creates) != len(source.extensions):
                raise PolicyError(
                    f"{source.path.name}: source/exact-render extension ownership mismatch"
                )

    findings = [
        f"revision graph accounted for {len(graph.revisions)} revision(s)",
        "cumulative upgrade and downgrade validated",
    ]
    if rendered_extensions:
        owner = next(source for source in sources.values() if source.extensions)
        findings.append(f"{owner.path.name}: approved extension ownership validated")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": True,
        "findings": findings,
        "errors": [],
        "revision_count": len(graph.revisions),
        "head": head,
        "evidence": {
            "cumulative_upgrade_rendered": True,
            "cumulative_downgrade_rendered": True,
        },
        "ddl_counts": _ddl_counts(upgrade.sql),
    }


def _failure_result(category: str) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": False,
        "findings": [],
        "errors": [category],
        "revision_count": 0,
        "head": None,
        "evidence": {
            "cumulative_upgrade_rendered": False,
            "cumulative_downgrade_rendered": False,
        },
        "ddl_counts": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--backend-root", required=True, type=Path)
    check.add_argument("--versions-dir", required=True, type=Path)
    check.add_argument("--alembic-config", required=True, type=Path)
    check.add_argument("--alembic", required=True, type=Path)
    check.add_argument("--render-timeout", required=True, type=int)
    args = parser.parse_args()
    try:
        result = check_repository(
            args.backend_root.resolve(strict=True),
            args.versions_dir.resolve(strict=True),
            args.alembic_config.resolve(strict=True),
            args.alembic.resolve(strict=True),
            args.render_timeout,
        )
    except PolicyError as exc:
        print(json.dumps(_failure_result(str(exc)), separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
