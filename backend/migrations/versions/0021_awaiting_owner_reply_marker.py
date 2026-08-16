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

import sqlalchemy as sa
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


# The ACTIVE (non-terminal) wait statuses — the partial-unique "one active wait"
# predicate. Derived from the enum so the index WHERE clause can never drift from
# PendingActionStatus (migration_parity asserts this alignment).
_ACTIVE_WAIT_STATUSES = (
    PendingActionStatus.AWAITING_OWNER_REPLY.value,
    PendingActionStatus.RESUME_REQUESTED.value,
)
_ACTIVE_WAIT_WHERE = _in_list("status", _ACTIVE_WAIT_STATUSES)
_MARKER_TYPE_WHERE = f"action_type = '{PendingActionType.AWAITING_OWNER_REPLY.value}'"

_CODE_UNIQUE_INDEX = "uq_pending_actions_business_correlation_code"
_ONE_ACTIVE_WAIT_INDEX = "uq_pending_actions_one_active_wait"
_CALL_FK = "fk_pending_actions_business_call"
_GUESS_TABLE = "owner_reply_guess_attempts"


def upgrade() -> None:
    # 1. Widen the two CHECK constraints to admit the new type + statuses.
    op.drop_constraint(_TYPE_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_TYPE_CONSTRAINT, _TABLE, _in_list("action_type", _ALL_TYPES))
    op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_STATUS_CONSTRAINT, _TABLE, _in_list("status", _ALL_STATUSES))

    # 2. Promote the marker index keys to real, typed columns (NULL for every
    #    other action type). The bounded owner ANSWER evidence stays in JSONB.
    op.add_column(_TABLE, sa.Column("call_id", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("query_type", sa.String(length=40), nullable=True))
    op.add_column(_TABLE, sa.Column("correlation_code", sa.String(length=12), nullable=True))
    # The caller's asked-about day, from trusted call state at escalation (P1
    # writes it, P3 reconciles against it). A trusted column, not JSONB — same
    # provenance class as call_id/query_type.
    op.add_column(_TABLE, sa.Column("target_date", sa.Date(), nullable=True))

    # 3. Composite (business_id, call_id) FK → calls(business_id, id). Binds a
    #    marker's call to a REAL call of the SAME business — a bare call_id FK
    #    would allow a cross-tenant reference. Enforces the trust boundary in
    #    schema.
    op.create_foreign_key(
        _CALL_FK,
        _TABLE,
        "calls",
        ["business_id", "call_id"],
        ["business_id", "id"],
    )

    # 4. Code uniqueness: unique per business across ALL retained marker rows
    #    (not active-only) — a historical code can't collide with a new one.
    #    Scoped to marker rows via the partial WHERE so other action types (NULL
    #    correlation_code) never participate.
    op.create_index(
        _CODE_UNIQUE_INDEX,
        _TABLE,
        ["business_id", "correlation_code"],
        unique=True,
        postgresql_where=text(_MARKER_TYPE_WHERE),
    )

    # 5. One active wait per (business, call, query_type): a second escalation on
    #    the same call+query can't create a duplicate wait while one is active.
    #    Scoped to the ACTIVE wait statuses (derived from the enum, not a drifting
    #    literal), so a resolved/expired marker frees the slot.
    op.create_index(
        _ONE_ACTIVE_WAIT_INDEX,
        _TABLE,
        ["business_id", "call_id", "query_type"],
        unique=True,
        postgresql_where=text(_ACTIVE_WAIT_WHERE),
    )

    # 6. Durable, tenant/owner-scoped code-guess counter (Dev3's P2 rate-limit
    #    writes here). DB-backed, NOT process-local: a per-replica in-memory
    #    counter would give an attacker N times the limit across N replicas. One row
    #    per (business_id, owner_phone) accumulates guesses within a window.
    op.create_table(
        _GUESS_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("owner_phone", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_owner_reply_guess_attempts_non_negative"),
        sa.CheckConstraint("max_attempts > 0", name="ck_owner_reply_guess_max_positive"),
        sa.UniqueConstraint(
            "business_id", "owner_phone", name="uq_owner_reply_guess_business_phone"
        ),
    )


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

    # Reverse of upgrade, in dependency order: guess table, indexes, FK, columns,
    # then re-narrow the CHECKs. The fail-closed guard above already proved no
    # marker rows remain, so dropping the marker columns/indexes is non-lossy.
    op.drop_table(_GUESS_TABLE)
    op.drop_index(_ONE_ACTIVE_WAIT_INDEX, table_name=_TABLE)
    op.drop_index(_CODE_UNIQUE_INDEX, table_name=_TABLE)
    op.drop_constraint(_CALL_FK, _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "target_date")
    op.drop_column(_TABLE, "correlation_code")
    op.drop_column(_TABLE, "query_type")
    op.drop_column(_TABLE, "call_id")

    op.drop_constraint(_TYPE_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _TYPE_CONSTRAINT, _TABLE, _in_list("action_type", _TYPES_WITHOUT_NEW)
    )
    op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _STATUS_CONSTRAINT, _TABLE, _in_list("status", _STATUSES_WITHOUT_NEW)
    )
