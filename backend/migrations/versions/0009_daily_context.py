"""daily_context

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-03

Adds business_daily_context table for owner-managed daily offers,
notes, and announcements that are injected into patient conversations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "business_daily_context",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("context_date", sa.Date(), nullable=False),
        sa.Column(
            "context_type",
            _enum(("offer", "note", "announcement"), "daily_context_type"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by_phone", sa.String(20), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )
    op.create_index(
        "ix_daily_context_lookup",
        "business_daily_context",
        ["business_id", "context_date", "active"],
    )


def downgrade() -> None:
    op.drop_index("ix_daily_context_lookup", table_name="business_daily_context")
    op.drop_table("business_daily_context")
