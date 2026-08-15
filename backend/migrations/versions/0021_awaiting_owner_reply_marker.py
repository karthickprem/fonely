"""Add the ``awaiting_owner_reply`` pending-action type + its wait statuses.

#43 owner-reply proactive resume. When the agent escalates to the owner
mid-call (asks availability it cannot confirm) and SUSPENDS the live call, it
leaves a durable correlation marker so an out-of-band owner reply — which
arrives in a SEPARATE process (the inbound worker) — can be tied back to the
exact suspended call and resumed. The marker is a ``pending_actions`` row with
``action_type='awaiting_owner_reply'`` and a wait lifecycle carried on the
SHARED ``status`` column:

    awaiting_owner_reply -> resume_requested -> resumed        (terminal)
                          \\-> (expired via the sweep = timed_out)
                          \\-> cancelled

The status MUST be the shared column, not payload-local: the authoritative
expiry sweep (``bulk_expire``) and the single-winner CAS (``conditional_update``)
both operate on shared ``status``. A payload-local wait status would be
invisible to the sweep (no authoritative timed_out) and would let the sweep's
EXPIRED and a resume's RESUMED arbitrate DIFFERENT fields — both could win, and
both would speak into the call. Shared status makes exactly one transition win.

Why CHECK-constraint edits and not ``ALTER TYPE ... ADD VALUE``: both
``action_type`` and ``status`` are NON-native enums — VARCHAR columns with named
CHECK constraints (``enum_type(..., native_enum=False, create_constraint=True)``).
Adding values is dropping and recreating the CHECK, fully transaction safe. The
allowed sets are DERIVED FROM the enums in code below so the constraints can
never drift from the models they mirror. Same pattern as 0019 (callback type).
"""

from alembic import context, op
from sqlalchemy import text

from fonely.models.enums import PendingActionStatus, PendingActionType

revision = "0021"
down_revision = "0020"

_TABLE = "pending_actions"
_TYPE_CONSTRAINT = "action_type"
_STATUS_CONSTRAINT = "pending_action_status"

# The values this migration ADDS. Downgrade removes exactly these, so a marker
# using any of them blocks a lossy downgrade (guarded below).
_NEW_TYPE = PendingActionType.AWAITING_OWNER_REPLY
_NEW_STATUSES = (
    PendingActionStatus.AWAITING_OWNER_REPLY,
    PendingActionStatus.RESUME_REQUESTED,
    PendingActionStatus.RESUMED,
)

# Derived from the enums so each constraint mirrors its model exactly — never a
# hand-typed value list that could drift.
_ALL_TYPES = tuple(m.value for m in PendingActionType)
_TYPES_WITHOUT_NEW = tuple(m.value for m in PendingActionType if m is not _NEW_TYPE)
_ALL_STATUSES = tuple(m.value for m in PendingActionStatus)
_STATUSES_WITHOUT_NEW = tuple(m.value for m in PendingActionStatus if m not in _NEW_STATUSES)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


def upgrade() -> None:
    op.drop_constraint(_TYPE_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_TYPE_CONSTRAINT, _TABLE, _in_list("action_type", _ALL_TYPES))
    op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_STATUS_CONSTRAINT, _TABLE, _in_list("status", _ALL_STATUSES))


def downgrade() -> None:
    # Fail closed: recreating either pre-0021 constraint while rows use the new
    # values would reject them (or force deleting suspended-call markers that
    # carry the caller's in-flight booking intent). Refuse and say so, exactly
    # like 0019/0018. Lock before counting so a marker created between the guard
    # and the swap cannot slip through.
    if not context.is_offline_mode():
        conn = op.get_bind()
        conn.execute(text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
        new_status_values = ", ".join(f"'{s.value}'" for s in _NEW_STATUSES)
        count = conn.execute(
            text(
                f"SELECT count(*) FROM {_TABLE} "
                f"WHERE action_type = '{_NEW_TYPE.value}' "
                f"   OR status IN ({new_status_values})"
            )
        ).scalar()
        if count:
            raise RuntimeError(
                f"refusing lossy downgrade: {_TABLE} holds {count} row(s) using the "
                "0021 owner-reply-resume type/statuses. Recreating the pre-0021 "
                "constraints would reject them (they carry a live caller's booking "
                "intent). Resolve or export those markers, delete them explicitly, "
                "then downgrade."
            )

    op.drop_constraint(_TYPE_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _TYPE_CONSTRAINT, _TABLE, _in_list("action_type", _TYPES_WITHOUT_NEW)
    )
    op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _STATUS_CONSTRAINT, _TABLE, _in_list("status", _STATUSES_WITHOUT_NEW)
    )
