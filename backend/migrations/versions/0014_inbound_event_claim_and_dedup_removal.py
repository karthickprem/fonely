"""Add claim infrastructure, update constraints, remove obsolete dedup table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add claim columns
    op.add_column(
        "whatsapp_inbound_events",
        sa.Column("claim_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "whatsapp_inbound_events",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "whatsapp_inbound_events",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "whatsapp_inbound_events",
        sa.Column(
            "claim_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "whatsapp_inbound_events",
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
    )

    # Update status constraint to include response_failed
    op.drop_constraint(
        "ck_whatsapp_inbound_status_valid",
        "whatsapp_inbound_events",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_status_valid",
        "whatsapp_inbound_events",
        "status IN ('received', 'processing', 'domain_processed', "
        "'completed', 'failed', 'dead_letter', 'response_failed')",
    )

    # Add phone_number_id nonempty constraint
    op.create_check_constraint(
        "ck_whatsapp_inbound_phone_number_id_nonempty",
        "whatsapp_inbound_events",
        "phone_number_id IS NULL OR length(phone_number_id) > 0",
    )

    # Remove obsolete dedup table
    op.drop_table("whatsapp_processed_messages")


def downgrade() -> None:
    # Recreate obsolete dedup table
    op.create_table(
        "whatsapp_processed_messages",
        sa.Column("message_id", sa.String(100), primary_key=True),
        sa.Column("business_id", sa.Integer, nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Remove phone_number_id nonempty constraint
    op.drop_constraint(
        "ck_whatsapp_inbound_phone_number_id_nonempty",
        "whatsapp_inbound_events",
    )

    # Restore original status constraint
    op.drop_constraint(
        "ck_whatsapp_inbound_status_valid",
        "whatsapp_inbound_events",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_status_valid",
        "whatsapp_inbound_events",
        "status IN ('received', 'processing', 'domain_processed', "
        "'completed', 'failed', 'dead_letter')",
    )

    # Remove claim columns
    op.drop_column("whatsapp_inbound_events", "provider_timestamp")
    op.drop_column("whatsapp_inbound_events", "claim_version")
    op.drop_column("whatsapp_inbound_events", "lease_expires_at")
    op.drop_column("whatsapp_inbound_events", "claimed_at")
    op.drop_column("whatsapp_inbound_events", "claim_token")
