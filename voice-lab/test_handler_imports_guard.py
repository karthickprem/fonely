"""Guard against connect-time NameErrors in the WebRTC event handlers.

The bug this catches: on_client_connected called `production_wiring.create_call_row`
but the module was only imported name-wise (`from production_wiring import
build_processors`), never as `import production_wiring`. Python raised NameError —
but ONLY when a real browser peer connected and the handler ran, AFTER the
WebRTC/ICE handshake succeeded. Ordinary import/collection of the module does not
execute the handler body, so a plain import smoke test misses it, and a live demo
caught it instead.

This AST check runs at test time (no server, no browser, no network): for each
transport event handler, every name used as a module attribute (`X.attr(...)`)
must be bound in the handler's scope — imported inside the handler, a parameter,
or a module-level import. If a handler references `foo.bar` with no binding for
`foo`, that's exactly the latent connect-time NameError, and this fails.

Cheap, deterministic, and it would have turned this demo incident into a red test.
"""

from __future__ import annotations

import ast
import os

_HANDLER_NAMES = {"on_client_connected", "on_client_disconnected"}
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.join(_THIS_DIR, "booking_pipeline.py")

# Names that are always available without an import (builtins + common globals we
# don't want to flag). Kept tight so real gaps aren't masked.
_ALWAYS_BOUND = {
    "self",
    "transport",
    "client",
    "logger",
    "print",
    "len",
    "str",
    "int",
    "float",
    "list",
    "dict",
    "range",
    "isinstance",
    "Exception",
}


def _module_level_bindings(tree: ast.Module) -> set[str]:
    """Names bound at module scope: top-level imports and assignments."""
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
    return bound


def _bindings_in_scope(fn: ast.AST) -> set[str]:
    """Names bound anywhere lexically inside a handler: local imports, assigned
    names, params, and nested def/class names. A superset is fine — we want to
    avoid false positives; the real gap (a module used but bound NOWHERE) still
    surfaces."""
    bound: set[str] = set()
    for x in ast.walk(fn):
        if isinstance(x, ast.Import):
            for a in x.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(x, ast.ImportFrom):
            for a in x.names:
                bound.add(a.asname or a.name)
        elif isinstance(x, ast.Assign):
            for t in x.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(x.name)
            bound.update(a.arg for a in x.args.args)
            bound.update(a.arg for a in x.args.kwonlyargs)
        elif isinstance(x, ast.arguments):
            bound.update(a.arg for a in x.args)
            bound.update(a.arg for a in x.kwonlyargs)
    return bound


def _module_attr_roots(fn: ast.AST) -> set[str]:
    """Root names used as `root.attr` (a module-attribute access) inside fn."""
    roots: set[str] = set()
    for x in ast.walk(fn):
        if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name):
            roots.add(x.value.id)
    return roots


def _enclosing_bindings(tree: ast.Module, target: ast.AST) -> set[str]:
    """Names bound in every function that lexically encloses `target`.

    A handler is nested inside run_booking_bot, so its closure sees that
    function's locals (worker, runner_args, the built services, etc). Those are
    legitimately available without an import — flagging them would be a false
    positive. Collect bindings from every ancestor function whose body contains
    the target."""
    bound: set[str] = set()

    def contains(node: ast.AST, needle: ast.AST) -> bool:
        return any(child is needle for child in ast.walk(node))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not target:
            if contains(node, target):
                bound |= _bindings_in_scope(node)
    return bound


def test_event_handlers_have_all_module_deps_imported():
    with open(_PIPELINE) as f:
        tree = ast.parse(f.read())

    module_bound = _module_level_bindings(tree)
    handlers = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in _HANDLER_NAMES
    ]
    assert handlers, "no transport event handlers found — did the file structure change?"

    problems: list[str] = []
    for fn in handlers:
        # In scope: the handler's own bindings, everything its enclosing
        # functions bind (closure), module-level names, and always-bound names.
        in_scope = (
            _bindings_in_scope(fn)
            | _enclosing_bindings(tree, fn)
            | module_bound
            | _ALWAYS_BOUND
        )
        for root in _module_attr_roots(fn):
            if root not in in_scope:
                problems.append(
                    f"{fn.name}: '{root}.<attr>' used but '{root}' is not bound "
                    f"in the handler scope, an enclosing scope, or at module level — "
                    f"this is a latent connect-time NameError"
                )

    assert not problems, "handler module-dependency gaps:\n" + "\n".join(problems)


if __name__ == "__main__":
    test_event_handlers_have_all_module_deps_imported()
    print("GUARD PASS: all handler module dependencies are bound in scope")
