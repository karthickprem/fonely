"""Portable import-origin guard for PostgreSQL integration tests.

Several offer/conversation tests must exercise THIS checkout's `backend/src`,
not the main-checkout src the shared `.venv` resolves when PYTHONPATH is unset
(the `.venv` symlinks to the main checkout, so `import fonely...` silently binds
to stale code unless `PYTHONPATH=$PWD/src` is set). The guard catches that
stale/external import at module load.

An earlier version asserted a hardcoded worktree name (`/dev3-dental-e2e/`) was
in the module's __file__. That is NOT portable: on the integration worktree the
import correctly resolves to a DIFFERENT checkout, so the literal falsely
rejected the right source and broke collection. This module replaces the branch
literal with a repo-relative check: the module must resolve under the SAME
`backend/src` tree as the test file, whatever worktree that is — true in any
checkout, false only for a genuinely external/stale import.
"""

from pathlib import Path
from types import ModuleType


def backend_root(test_file: str) -> Path:
    """The backend/ root of the checkout containing `test_file`.

    Test files live at backend/tests/integration/postgres/<file>.py, so backend/
    is parents[3] of the file: [0]=postgres, [1]=integration, [2]=tests,
    [3]=backend.
    """
    return Path(test_file).resolve().parents[3]


def is_module_from_checkout(module: ModuleType, test_file: str) -> bool:
    """True iff `module` resolves under `test_file`'s checkout backend/src.

    Pure predicate (no assert) so it can be unit-tested in both directions —
    the negative case is what keeps the guard's teeth.
    """
    module_file = getattr(module, "__file__", None)
    if module_file is None:
        return False
    src_root = backend_root(test_file) / "src"
    return Path(module_file).resolve().is_relative_to(src_root)


def assert_module_from_this_checkout(module: ModuleType, test_file: str) -> None:
    """Fail at import if `module` did not resolve from this checkout's backend/src.

    Portable across worktrees and CI; still fails on a stale/external import
    (e.g. the shared .venv's main-checkout src when PYTHONPATH is unset).
    """
    if not is_module_from_checkout(module, test_file):
        resolved = getattr(module, "__file__", "<no __file__>")
        src_root = backend_root(test_file) / "src"
        raise AssertionError(
            f"{getattr(module, '__name__', module)!r} resolved from {resolved!r} — "
            f"outside this checkout's src ({src_root}). This is a stale/external "
            "import: run with PYTHONPATH=$PWD/src so the test exercises the code "
            "under test, not the shared .venv's main-checkout copy."
        )
