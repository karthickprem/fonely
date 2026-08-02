"""notification_outbox

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-02

Adds the transactional notification outbox table for guaranteed delivery
of appointment confirmation and clinic alert notifications.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AFFECTED_TABLES = (
    "businesses",
    "notification_outbox",
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


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    _lock_affected_tables()

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column(
            "event_type",
            _enum(
                (
                    "appointment_confirmed",
                    "appointment_cancelled",
                    "appointment_rescheduled",
                    "appointment_reminder",
                ),
                "notification_event_type",
            ),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column(
            "recipient_type",
            _enum(("patient", "owner", "staff"), "notification_recipient_type"),
            nullable=False,
        ),
        sa.Column("recipient_phone", sa.String(20), nullable=False),
        sa.Column("recipient_name", sa.String(200), nullable=True),
        sa.Column(
            "channel",
            _enum(("whatsapp", "sms", "internal"), "notification_channel"),
            nullable=False,
        ),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            _enum(
                ("pending", "processing", "delivered", "failed", "dead_letter"),
                "notification_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
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
        sa.UniqueConstraint("idempotency_key", name="uq_notification_idempotency"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
    )
    op.create_index(
        "ix_notification_outbox_poll",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_entity",
        "notification_outbox",
        ["business_id", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    _lock_affected_tables()
    op.drop_index("ix_notification_outbox_entity", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_poll", table_name="notification_outbox")
    op.drop_table("notification_outbox")
