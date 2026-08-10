"""owner_command_proposals

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-10

Adds the owner_command_proposals table for durable, idempotent owner
commands (close_clinic, close_early, doctor_leave) with confirmation
workflow and atomicity guarantees.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AFFECTED_TABLES = (
    "businesses",
    "business_users",
    "owner_command_proposals",
)


def _lock_affected_tables() -> None:
    table_names = ", ".join(f"'{t}'" for t in _AFFECTED_TABLES)
    op.execute(
        f"""DO $migration_lock$
        DECLARE
            table_name text;
        BEGIN
            FOREACH table_name IN ARRAY ARRAY[{table_names}] LOOP
                IF to_regclass(format('%I.%I', current_schema(), table_name)) IS NOT NULL THEN
                    EXECUTE format(
                        'LOCK TABLE %I.%I IN SHARE ROW EXCLUSIVE MODE',
                        current_schema(), table_name
                    );
                END IF;
            END LOOP;
        END
        $migration_lock$"""
    )


def upgrade() -> None:
    _lock_affected_tables()

    # Add composite unique constraint on business_users needed for the
    # composite FK from owner_command_proposals.
    op.create_unique_constraint(
        "uq_business_users_business_id",
        "business_users",
        ["business_id", "id"],
    )

    op.create_table(
        "owner_command_proposals",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("owner_phone_snapshot", sa.String(20), nullable=False),
        sa.Column("command_type", sa.String(40), nullable=False),
        sa.Column(
            "command_payload",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "preview_snapshot",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="pending_confirmation",
        ),
        sa.Column(
            "result_evidence",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "expected_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failure_message", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "owner_user_id"],
            ["business_users.business_id", "business_users.id"],
            name="fk_owner_proposal_business_user",
        ),
        sa.UniqueConstraint(
            "business_id",
            "idempotency_key",
            name="uq_owner_proposal_idempotency",
        ),
        sa.CheckConstraint(
            "command_type IN ('close_clinic', 'close_early', 'doctor_leave')",
            name="ck_owner_proposal_command_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending_confirmation', 'confirmed', 'executing', "
            "'completed', 'rejected', 'expired', 'failed')",
            name="ck_owner_proposal_status",
        ),
        sa.CheckConstraint(
            "expected_version > 0",
            name="ck_owner_proposal_expected_version",
        ),
    )

    # Populated-safe preflight: before creating the partial unique index,
    # verify no duplicate pending proposals exist per owner.
    op.execute(
        "DO $preflight$ BEGIN "
        "IF EXISTS ("
        "SELECT business_id, owner_user_id "
        "FROM owner_command_proposals "
        "WHERE status = 'pending_confirmation' "
        "GROUP BY business_id, owner_user_id "
        "HAVING count(*) > 1"
        ") THEN "
        "RAISE EXCEPTION '0015 upgrade blocked: duplicate pending proposals "
        "exist per owner — resolve before applying partial unique index'; "
        "END IF; "
        "END $preflight$"
    )

    # Partial unique index: at most one pending proposal per owner per business
    op.create_index(
        "uq_owner_proposal_owner_pending",
        "owner_command_proposals",
        ["business_id", "owner_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_confirmation'"),
    )

    # Operational index for expiry sweeper
    op.create_index(
        "ix_owner_proposal_pending_expiry",
        "owner_command_proposals",
        ["status", "expires_at"],
        postgresql_where=sa.text("status = 'pending_confirmation'"),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE owner_command_proposals IN ACCESS EXCLUSIVE MODE")

    # Fail-closed populated downgrade guard: refuse if any rows carry
    # result_evidence that cannot be represented without this table.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM owner_command_proposals "
        "WHERE result_evidence IS NOT NULL) THEN "
        "RAISE EXCEPTION '0015 downgrade blocked: "
        "rows with result_evidence exist and cannot be dropped'; "
        "END IF; END $$"
    )

    op.drop_index(
        "ix_owner_proposal_pending_expiry",
        table_name="owner_command_proposals",
    )
    op.drop_index(
        "uq_owner_proposal_owner_pending",
        table_name="owner_command_proposals",
    )
    op.drop_table("owner_command_proposals")

    op.drop_constraint(
        "uq_business_users_business_id",
        "business_users",
        type_="unique",
    )
