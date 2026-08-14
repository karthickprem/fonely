"""Add the ``callback`` pending-action type.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-13

When the agent gives up mid-booking on a VOICE call — it could not disambiguate
which doctor or which slot the caller meant, and the terminating ladder ran out
— #33 fixed the WORDING (no false "call the clinic" on a live call), but nothing
was PERSISTED. The caller was told "no appointment made" and that was the end:
no record, no way for anyone to follow up. This type is the durable follow-up
record, carrying the partial booking facts a human needs to call the caller back
and complete the booking.

Why a CHECK-constraint edit and not ``ALTER TYPE ... ADD VALUE``:

    ``pending_actions.action_type`` is NOT a native PostgreSQL enum. It is a
    VARCHAR with a named CHECK constraint (the app persists StrEnum values via
    ``enum_type(..., native_enum=False, create_constraint=True)``). So the
    Postgres ``ALTER TYPE ADD VALUE`` gotcha (cannot run inside a transaction
    block in older patterns) does not apply here at all. Adding a value is
    dropping and recreating the CHECK constraint, which is fully transaction
    safe. The allowed set is derived from ``PendingActionType`` in code below so
    the constraint can never drift from the enum it mirrors.

The constraint is named ``action_type`` (it takes the Enum's ``name=``), matching
what SQLAlchemy emitted in 0001.
"""

from alembic import context, op
from sqlalchemy import text

from fonely.models.enums import PendingActionType

revision = "0019"
down_revision = "0018"

_CONSTRAINT = "action_type"
_TABLE = "pending_actions"

# Derived from the enum so the constraint mirrors the model exactly — never a
# hand-typed value list that could drift from PendingActionType.
_ALL_VALUES = tuple(member.value for member in PendingActionType)
_WITHOUT_CALLBACK = tuple(
    member.value for member in PendingActionType if member is not PendingActionType.CALLBACK
)


def _in_list_check(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"action_type IN ({joined})"


def upgrade() -> None:
    # Drop the 5-value constraint, recreate it including 'callback'. The value
    # set comes from PendingActionType (which now includes CALLBACK).
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _in_list_check(_ALL_VALUES))


def downgrade() -> None:
    # Fail closed: recreating the constraint WITHOUT 'callback' while callback
    # rows exist would either error on constraint creation or, worse, require
    # deleting patient-PII-bearing callback records to proceed. Refuse and say
    # so, exactly like 0018 refuses to drop DPDP evidence. Lock before counting
    # so a callback created between the guard and the swap cannot slip through.
    if not context.is_offline_mode():
        conn = op.get_bind()
        conn.execute(text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
        count = conn.execute(
            text(f"SELECT count(*) FROM {_TABLE} WHERE action_type = 'callback'")
        ).scalar()
        if count:
            raise RuntimeError(
                f"refusing lossy downgrade: {_TABLE} holds {count} row(s) with "
                "action_type='callback'. Recreating the pre-0019 constraint would "
                "reject them. Resolve or export those callbacks (they carry caller "
                "PII and booking intent), delete them explicitly, then downgrade."
            )

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _in_list_check(_WITHOUT_CALLBACK))
