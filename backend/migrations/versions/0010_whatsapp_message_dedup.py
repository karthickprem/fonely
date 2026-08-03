"""WhatsApp message deduplication table.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("whatsapp_processed_messages")
