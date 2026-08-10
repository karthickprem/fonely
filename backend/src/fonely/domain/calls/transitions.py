"""Forward-only call status transition validation.

Provider-neutral: uses canonical call status strings with underscores.
Adapters normalize provider-specific formats (hyphens, etc).
"""

from __future__ import annotations

QUEUED = "queued"
RINGING = "ringing"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
BUSY = "busy"
NO_ANSWER = "no_answer"

_TERMINAL = frozenset({COMPLETED, FAILED, BUSY, NO_ANSWER})

_ALL_STATUSES = frozenset(
    {QUEUED, RINGING, IN_PROGRESS, COMPLETED, FAILED, BUSY, NO_ANSWER}
)

_ALLOWED_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: _ALL_STATUSES,
    QUEUED: _ALL_STATUSES - {QUEUED},
    RINGING: frozenset({IN_PROGRESS}) | _TERMINAL,
    IN_PROGRESS: _TERMINAL,
}
for _t in _TERMINAL:
    _ALLOWED_TRANSITIONS[_t] = frozenset()


class InvalidCallTransitionError(Exception):
    def __init__(self, current: str | None, attempted: str) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(f"invalid call transition: {current!r} -> {attempted!r}")


class LateCallEventError(Exception):
    """A lower-state event arrived after a terminal status — harmless no-op."""

    def __init__(self, current: str, attempted: str) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(f"late event after terminal: {current!r} ignored {attempted!r}")


def validate_transition(current_status: str | None, new_status: str) -> str:
    if current_status is not None and current_status == new_status:
        return current_status

    if current_status in _TERMINAL:
        if new_status not in _TERMINAL:
            raise LateCallEventError(current_status, new_status)
        raise InvalidCallTransitionError(current_status, new_status)

    allowed = _ALLOWED_TRANSITIONS.get(current_status)
    if allowed is None or new_status not in allowed:
        raise InvalidCallTransitionError(current_status, new_status)

    return new_status


def is_terminal(status: str) -> bool:
    return status in _TERMINAL
