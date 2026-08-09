"""Durable WhatsApp inbound event queue.

Replaces BackgroundTasks pattern — messages are persisted before
returning 200 to Meta, then processed by a worker with retry.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_inbound_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(100), nullable=False),
        sa.UniqueConstraint("message_id", name="uq_whatsapp_inbound_message_id"),
        sa.Column(
            "business_id",
            sa.Integer,
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column("sender_phone", sa.String(20), nullable=False),
        sa.Column("message_type", sa.String(20), nullable=False),
        sa.Column("message_body", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_whatsapp_inbound_events_poll",
        "whatsapp_inbound_events",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.execute("LOCK TABLE whatsapp_inbound_events IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM whatsapp_inbound_events) THEN "
        "RAISE EXCEPTION '0012 downgrade blocked: "
        "durable inbox history exists and cannot be truthfully represented "
        "by legacy dedup tombstones alone'; "
        "END IF; END $$"
    )
    op.drop_index(
        "ix_whatsapp_inbound_events_poll",
        table_name="whatsapp_inbound_events",
    )
    op.drop_table("whatsapp_inbound_events")
