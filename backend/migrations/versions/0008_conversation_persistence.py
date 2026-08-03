"""conversation_persistence

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-03

Adds conversation and conversation_turns tables for durable booking
conversation state that survives server restarts and multi-worker
deployments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AFFECTED_TABLES = (
    "conversation_turns",
    "conversations",
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

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("customer_phone", sa.String(20), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column(
            "collected_facts",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("proposal_id", sa.Integer(), nullable=True),
        sa.Column("proposal_version", sa.Integer(), nullable=True),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )
    op.create_index(
        "ix_conversations_phone_lookup",
        "conversations",
        ["business_id", "customer_phone", "state"],
    )
    op.create_index("ix_conversations_expiry", "conversations", ["expires_at"])

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("intent", sa.String(30), nullable=False),
        sa.Column("safety_classification", sa.String(20), nullable=False),
        sa.Column("user_message_hash", sa.String(64), nullable=False),
        sa.Column("assistant_response", sa.Text(), nullable=False),
        sa.Column(
            "collected_facts_snapshot",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "missing_facts",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("proposal_id", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )
    op.create_index(
        "ix_conversation_turns_lookup",
        "conversation_turns",
        ["conversation_id", "turn_number"],
    )


def downgrade() -> None:
    _lock_affected_tables()
    op.drop_index("ix_conversation_turns_lookup", table_name="conversation_turns")
    op.drop_table("conversation_turns")
    op.drop_index("ix_conversations_expiry", table_name="conversations")
    op.drop_index("ix_conversations_phone_lookup", table_name="conversations")
    op.drop_table("conversations")
