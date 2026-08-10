"""Add business_users composite unique and owner_command_proposals table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- business_users composite unique for FK target -------------------------
    op.create_unique_constraint(
        "uq_business_users_business_id",
        "business_users",
        ["business_id", "id"],
    )

    # -- owner_command_proposals -----------------------------------------------
    op.create_table(
        "owner_command_proposals",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("business_id", sa.Integer, sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("owner_user_id", sa.Integer, nullable=False),
        sa.Column("command_type", sa.String(50), nullable=False),
        sa.Column("command_payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("expected_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("result_summary", postgresql.JSONB, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "owner_user_id"],
            ["business_users.business_id", "business_users.id"],
            name="fk_owner_proposal_business_user",
        ),
        sa.CheckConstraint(
            "status IN ('pending_confirmation', 'rejected', 'expired', 'completed')",
            name="ck_owner_proposal_status",
        ),
    )

    op.create_unique_constraint(
        "uq_owner_proposal_idempotency",
        "owner_command_proposals",
        ["business_id", "idempotency_key"],
    )

    op.create_index(
        "ix_owner_proposal_pending",
        "owner_command_proposals",
        ["business_id", "owner_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_confirmation'"),
    )

    op.create_index(
        "ix_owner_proposal_expiry",
        "owner_command_proposals",
        ["status", "expires_at"],
    )


def _preflight_downgrade() -> None:
    """Fail-closed: refuse lossy downgrade if proposals exist."""
    bind = op.get_bind()
    result = bind.execute(sa.text("SELECT 1 FROM owner_command_proposals LIMIT 1"))
    if result.scalar_one_or_none() is not None:
        raise RuntimeError(
            "Migration 0015 downgrade blocked: owner_command_proposals table "
            "contains data. Manual cleanup required."
        )


def downgrade() -> None:
    try:
        _preflight_downgrade()
    except AttributeError:
        # Offline / test recorder — emit a guard SQL instead
        op.execute(
            sa.text(
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM owner_command_proposals LIMIT 1) "
                "THEN CAST('Migration 0015 downgrade requires empty owner_command_proposals' "
                "AS integer) END"
            )
        )
    op.drop_index("ix_owner_proposal_expiry", table_name="owner_command_proposals")
    op.drop_index("ix_owner_proposal_pending", table_name="owner_command_proposals")
    op.drop_constraint("uq_owner_proposal_idempotency", "owner_command_proposals")
    op.drop_table("owner_command_proposals")
    op.drop_constraint("uq_business_users_business_id", "business_users")
