"""Forward-only call status transition validation."""

from __future__ import annotations

from fonely.domain.calls.events import ExotelCallStatus

_TERMINAL = frozenset(
    {
        ExotelCallStatus.COMPLETED,
        ExotelCallStatus.FAILED,
        ExotelCallStatus.BUSY,
        ExotelCallStatus.NO_ANSWER,
    }
)

_ALLOWED_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset(ExotelCallStatus),
    ExotelCallStatus.QUEUED: frozenset(ExotelCallStatus) - {ExotelCallStatus.QUEUED},
    ExotelCallStatus.IN_PROGRESS: _TERMINAL,
}
for _t in _TERMINAL:
    _ALLOWED_TRANSITIONS[_t] = frozenset()


class InvalidCallTransitionError(Exception):
    def __init__(self, current: str | None, attempted: str) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(f"invalid call transition: {current!r} -> {attempted!r}")


def validate_transition(current_status: str | None, new_status: str) -> str:
    """Return the new status if the transition is valid.

    A terminal status cannot be overwritten. A duplicate terminal
    callback for the same status is an idempotent no-op (returns
    the current status unchanged). A different terminal status
    after an existing terminal is rejected.
    """
    if current_status is not None and current_status == new_status:
        return current_status

    allowed = _ALLOWED_TRANSITIONS.get(current_status)
    if allowed is None or new_status not in allowed:
        raise InvalidCallTransitionError(current_status, new_status)

    return new_status


def is_terminal(status: str) -> bool:
    return status in _TERMINAL
