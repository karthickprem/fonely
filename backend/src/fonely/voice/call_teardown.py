"""Exactly-once release of a call's admission slot.

An admission slot leaked on one path and double-released on another both end the
call looking normal from the outside — and the second one silently reduces
capacity until the process restarts. So the release must run EXACTLY once
regardless of how the call ends: normal completion, an exception raised inside
the pipeline, task cancellation, a timeout, socket disconnect, or shutdown.

``AdmissionController.release`` clamps at zero but does not self-guard against
releasing a slot twice, so the exactly-once guarantee lives here, per call: a
single guarded wrapper the runtime installs in one ``finally`` and calls on
every terminal path. The flag makes repeat calls no-ops; the count of underlying
releases is therefore always one.
"""

from __future__ import annotations

from collections.abc import Callable


class OnceRelease:
    """Wraps a zero-arg release callable so it fires at most once.

    Construct with the exact release action for this call (e.g.
    ``lambda: admission.release(tenant_id)``). Call ``release()`` from every
    terminal path — normal, error, cancel, timeout, shutdown — and from the
    outer ``finally``; only the first call reaches the underlying release.
    """

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._release()
