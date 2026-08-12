"""Shared PostgreSQL concurrency-observation helper (CEO #15).

Two independent concurrency tests each grew their OWN observer that asserted a
blocked backend's `wait_event_type` on a SINGLE sample of pg_stat_activity. That
view is sampled: a backend is visibly blocked (`pg_blocking_pids` already lists
the blocker) one instant BEFORE its wait-event fields are populated. Asserting
both are simultaneous makes the test intermittently fail under load — a liveness
race in the harness, not a product defect. The two copies had already diverged
(one sampled an extra `wait_event` column) while both were wrong the same way,
which is exactly why this lives in ONE place now.

The correct contract:
  - poll until the blocker is observed AND `wait_event_type` is non-null;
  - keep the `wait_event_type == "Lock"` assertion — a backend can block for
    reasons other than the lock under test, so dropping it makes the test pass
    for the wrong reason;
  - on deadline expiry, FAIL with a message that distinguishes "we never
    observed the wait event" from "no lock was ever taken" — never-observed and
    no-lock are different facts and must not collapse into one string.
"""

import asyncio
import os
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Margin instrumentation (CEO #15 rule 2): every successful observation records
# the lag between "blocker first observed" and "wait_event_type populated" — the
# exact sampling window the old single-sample assertion raced against. The
# deadline must be chosen from this distribution, not from taste: a 5s deadline
# that passes 20/20 while the worst lag is 4.8s is a coin flip, not a margin.
# Appending to a file keeps the data across separate pytest processes (each 20x
# run is its own process); the path is opt-in so normal runs pay nothing.
_LAG_LOG_PATH = os.environ.get("FONELY_CONCURRENCY_LAG_LOG")


def _record_lag(observe_to_wait_event_seconds: float) -> None:
    if _LAG_LOG_PATH:
        with open(_LAG_LOG_PATH, "a") as fh:
            fh.write(f"{observe_to_wait_event_seconds:.6f}\n")


async def install_transaction_timeouts(session: AsyncSession) -> None:
    """Bound every observer/contender session so a hang fails fast, loudly."""
    await session.execute(text("SET LOCAL lock_timeout = '8s'"))
    await session.execute(text("SET LOCAL statement_timeout = '15s'"))
    await session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15s'"))


class _NeverObservedWaitEventError(AssertionError):
    """The blocker was seen but its wait-event never populated within the
    deadline. DISTINCT from a no-lock failure: the lock contention DID happen
    (pg_blocking_pids listed the blocker); only the sampled wait-event fields
    stayed null long enough to exceed the deadline. This is the harness's own
    sampling race, not a product ordering violation.

    RULE (do not weaken when this fires in CI): this MUST stay a hard FAILURE.
    Never mark it skip, xfail, warning, or retry — the obvious first move to get
    a green board is "this one's flaky, mute it", and that converts a real signal
    back into silence. Muting it is how tests we cite as evidence quietly stop
    proving anything (see the eight dead postgres files that read as passing for
    a week). If this fires, INVESTIGATE — a genuinely-observed blocker whose wait
    event never appears within a data-chosen deadline is telling us the sampled
    view is lagging far more than measured, which is itself worth knowing. Do not
    silence the messenger; widen the deadline only if the measured margin data
    (see observe_lock_contention's recorded lag) justifies it."""


class _BlockerNeverObservedError(AssertionError):
    """No lock was ever observed: the expected blocker never appeared in
    pg_blocking_pids for the blocked backend within the deadline. DISTINCT from
    _NeverObservedWaitEventError — this says the contention itself did not happen
    (or was not observable), which would implicate the product, not the sample."""


async def observe_lock_contention(
    factory: async_sessionmaker[AsyncSession],
    *,
    blocked_pid: int,
    expected_blocker_pid: int,
    deadline_seconds: float = 5.0,
) -> str:
    """Wait until `expected_blocker_pid` is observed blocking `blocked_pid` on a
    lock, tolerating the pg_stat_activity sampling lag. Returns the observed
    `wait_event` string.

    Two independent deadlines, so the two failure modes stay distinguishable:
      - the blocker is never observed at all      -> _BlockerNeverObservedError
      - the blocker is observed but its wait_event -> _NeverObservedWaitEventError
        never populates (the sampling race)

    Keeps the `wait_event_type == 'Lock'` assertion: a backend blocked for a
    non-lock reason is a real problem and must still fail here.
    """
    start = time.monotonic()
    blocker_first_observed_at: float | None = None
    last_row: tuple[object, ...] | None = None

    while time.monotonic() - start < deadline_seconds:
        async with factory() as observer:
            await install_transaction_timeouts(observer)
            row = (
                await observer.execute(
                    text(
                        "SELECT :blocker = ANY(pg_blocking_pids(:blocked)), "
                        "wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :blocked"
                    ),
                    {"blocker": expected_blocker_pid, "blocked": blocked_pid},
                )
            ).one_or_none()
        last_row = tuple(row) if row is not None else None

        if row is not None and row[0] is True:
            if blocker_first_observed_at is None:
                blocker_first_observed_at = time.monotonic()
            # The blocker is observed. Only NOW is the wait-event meaningful; a
            # null here is the sampling lag, so keep polling rather than asserting
            # on this single sample.
            if row[1] is not None:
                assert row[1] == "Lock", (
                    f"blocker observed but wait_event_type={row[1]!r} "
                    f"(expected 'Lock'), wait_event={row[2]!r}, "
                    f"blocked_pid={blocked_pid}, blocker_pid={expected_blocker_pid}, "
                    f"elapsed={time.monotonic() - start:.2f}s"
                )
                assert row[2] is not None, (
                    f"wait_event_type is 'Lock' but wait_event is null — "
                    f"blocked_pid={blocked_pid}, blocker_pid={expected_blocker_pid}"
                )
                # Margin data: how long the wait-event sample lagged behind the
                # blocker becoming visible. This is the exact window the deadline
                # must clear; report its distribution to choose the deadline.
                _record_lag(time.monotonic() - blocker_first_observed_at)
                return str(row[2])
        await asyncio.sleep(0.01)

    elapsed = time.monotonic() - start
    if blocker_first_observed_at is not None:
        raise _NeverObservedWaitEventError(
            f"blocker WAS observed blocking pid {blocked_pid} but its wait_event "
            f"never populated within {elapsed:.2f}s — this is the pg_stat_activity "
            f"sampling race, NOT a missing lock. last_row={last_row!r}, "
            f"blocker_pid={expected_blocker_pid}"
        )
    raise _BlockerNeverObservedError(
        f"expected blocker pid {expected_blocker_pid} was NEVER observed blocking "
        f"pid {blocked_pid} within {elapsed:.2f}s — the lock contention did not "
        f"happen or was not observable. last_row={last_row!r}"
    )
