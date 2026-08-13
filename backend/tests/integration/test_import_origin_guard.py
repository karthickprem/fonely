"""Teeth for the portable import-origin guard (postgres/import_origin.py).

The portability fix must NOT be so permissive it never catches stale code — that
would be a wrong-reason-green (a guard that always passes proves nothing). These
tests prove both directions: the guard ACCEPTS a module resolved from a test
file's own checkout backend/src, and REJECTS one resolved from any other tree
(the stale/external import it exists to catch).

Deliberately placed OUTSIDE tests/integration/postgres/ so it runs in the fast
(non-DB) suite: the postgres/ package has autouse fixtures that skip the whole
directory when FONELY_TEST_DATABASE_URL is unset, which would hide this pure-path
guard behind a DB requirement it does not have. The tests use SYNTHETIC paths
(never their own __file__), so they verify the helper's parents[3] contract for a
tests/integration/postgres/<file> caller regardless of where this test lives.
"""

from pathlib import Path

import pytest

from tests.integration.postgres.import_origin import (
    assert_module_from_this_checkout,
    backend_root,
    is_module_from_checkout,
)

# A synthetic PG-test-file path at the exact depth the guard is used from:
# <root>/backend/tests/integration/postgres/<file>.py — so parents[3] == backend.
_CHECKOUT_A = "/scratch/x/fonely/.claude/worktrees/wt-a/backend"
_TEST_FILE_A = f"{_CHECKOUT_A}/tests/integration/postgres/test_x_postgres.py"
_CHECKOUT_B = "/scratch/y/fonely/.claude/worktrees/wt-b/backend"


def _fake_module(file_path: str | None):
    """A stand-in with a controlled __file__/__name__, so the guard can be pointed
    at an arbitrary resolved path without importing anything real."""
    return type("M", (), {"__file__": file_path, "__name__": "fake.module"})()


def test_backend_root_is_parents3_of_a_pg_test_file() -> None:
    # tests/integration/postgres/<file> -> [0]=postgres [1]=integration
    # [2]=tests [3]=backend. The guard's contract; assert it explicitly.
    assert backend_root(_TEST_FILE_A) == Path(_CHECKOUT_A)


def test_accepts_module_under_the_test_files_own_checkout_src() -> None:
    inside = _fake_module(f"{_CHECKOUT_A}/src/fonely/domain/booking/offers.py")
    assert is_module_from_checkout(inside, _TEST_FILE_A) is True
    assert_module_from_this_checkout(inside, _TEST_FILE_A)  # must not raise


def test_rejects_module_from_a_different_checkout() -> None:
    # The exact defect this replaces: on the integration worktree the import
    # resolves to a DIFFERENT checkout's src. That must be REJECTED.
    other = _fake_module(f"{_CHECKOUT_B}/src/fonely/domain/booking/offers.py")
    assert is_module_from_checkout(other, _TEST_FILE_A) is False
    with pytest.raises(AssertionError, match="stale/external import"):
        assert_module_from_this_checkout(other, _TEST_FILE_A)


def test_rejects_main_checkout_src_the_shared_venv_resolves() -> None:
    # The concrete stale-import case the guard exists for: the shared .venv
    # resolves fonely from the MAIN checkout (…/fonely/backend/src, NOT under any
    # worktree). For a test file in a worktree, that path is outside its src and
    # must be rejected — precisely what PYTHONPATH=$PWD/src prevents.
    main = _fake_module("/scratch/x/fonely/backend/src/fonely/domain/booking/offers.py")
    assert is_module_from_checkout(main, _TEST_FILE_A) is False


def test_rejects_module_with_no_file() -> None:
    # A namespace/extension module with no __file__ cannot be proven in-tree.
    assert is_module_from_checkout(_fake_module(None), _TEST_FILE_A) is False


def test_guard_is_not_vacuous_accept_and_reject_disagree() -> None:
    # Belt against an always-true guard: the SAME module path is accepted for its
    # own checkout and rejected for a foreign one. If both returned True the guard
    # would be decorative.
    mod = _fake_module(f"{_CHECKOUT_A}/src/fonely/x.py")
    assert is_module_from_checkout(mod, _TEST_FILE_A) is True
    foreign_test = f"{_CHECKOUT_B}/tests/integration/postgres/test_y_postgres.py"
    assert is_module_from_checkout(mod, foreign_test) is False
