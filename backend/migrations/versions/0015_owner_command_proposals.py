"""Add owner_command_proposals table for durable two-phase owner confirmation.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_COMMAND_TYPES = ("close_clinic", "close_early", "doctor_leave")
_STATUSES = (
    "pending_confirmation",
    "confirmed",
    "executing",
    "completed",
    "rejected",
    "expired",
    "failed",
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_business_users_business_id_id",
        "business_users",
        ["business_id", "id"],
    )

    op.create_table(
        "owner_command_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "business_id",
            sa.Integer,
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.Integer, nullable=False),
        sa.Column("owner_phone_snapshot", sa.String(20), nullable=False),
        sa.Column("command_type", sa.String(40), nullable=False),
        sa.Column(
            "command_payload",
            sa.dialects.postgresql.JSONB,
            nullable=False,
        ),
        sa.Column(
            "preview_snapshot",
            sa.dialects.postgresql.JSONB,
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
            "expected_version",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column("result_evidence", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
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
            "expected_version > 0",
            name="ck_owner_proposal_version_positive",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES) + ")",
            name="ck_owner_proposal_status",
        ),
        sa.CheckConstraint(
            "command_type IN (" + ", ".join(f"'{t}'" for t in _COMMAND_TYPES) + ")",
            name="ck_owner_proposal_command_type",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0",
            name="ck_owner_proposal_idempotency_nonempty",
        ),
        sa.CheckConstraint(
            "length(owner_phone_snapshot) > 0",
            name="ck_owner_proposal_phone_nonempty",
        ),
        sa.CheckConstraint(
            "length(payload_digest) > 0",
            name="ck_owner_proposal_digest_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(command_payload) = 'object'",
            name="ck_owner_proposal_payload_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(preview_snapshot) = 'object'",
            name="ck_owner_proposal_snapshot_object",
        ),
        sa.CheckConstraint(
            "(status != 'completed') OR (completed_at IS NOT NULL AND result_evidence IS NOT NULL)",
            name="ck_owner_proposal_completed_requires_evidence",
        ),
        sa.CheckConstraint(
            "(status != 'confirmed') OR (confirmed_at IS NOT NULL)",
            name="ck_owner_proposal_confirmed_requires_timestamp",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('failed')) OR (failure_code IS NOT NULL)",
            name="ck_owner_proposal_failed_requires_code",
        ),
    )

    op.create_index(
        "ix_owner_proposal_owner_pending",
        "owner_command_proposals",
        ["business_id", "owner_user_id", "status"],
        postgresql_where="status = 'pending_confirmation'",
    )

    op.create_index(
        "ix_owner_proposal_expiry",
        "owner_command_proposals",
        ["status", "expires_at"],
        postgresql_where="status = 'pending_confirmation'",
    )


def downgrade() -> None:
    op.execute("LOCK TABLE owner_command_proposals IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM owner_command_proposals LIMIT 1) THEN "
        "RAISE EXCEPTION USING "
        "MESSAGE = 'Cannot downgrade 0015: owner command proposal evidence exists', "
        "ERRCODE = 'check_violation'; "
        "END IF; "
        "END $$"
    )
    op.drop_index("ix_owner_proposal_expiry", table_name="owner_command_proposals")
    op.drop_index("ix_owner_proposal_owner_pending", table_name="owner_command_proposals")
    op.drop_table("owner_command_proposals")
    op.drop_constraint("uq_business_users_business_id_id", "business_users", type_="unique")
