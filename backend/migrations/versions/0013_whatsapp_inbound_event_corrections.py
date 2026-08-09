"""Add phone_number_id, dead_lettered_at, and check constraints.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _notif_enum(*values: str) -> sa.Enum:
    return sa.Enum(
        *values,
        name="notification_event_type",
        native_enum=False,
        create_constraint=True,
    )


_NEW_EVENT_TYPES = (
    "appointment_confirmed",
    "appointment_cancelled",
    "appointment_rescheduled",
    "appointment_reminder",
    "whatsapp_inbound_response",
)

_OLD_EVENT_TYPES = (
    "appointment_confirmed",
    "appointment_cancelled",
    "appointment_rescheduled",
    "appointment_reminder",
)


def upgrade() -> None:
    # Widen notification_outbox.event_type for new whatsapp_inbound_response value
    op.drop_constraint("notification_event_type", "notification_outbox")
    op.alter_column(
        "notification_outbox",
        "event_type",
        type_=_notif_enum(*_NEW_EVENT_TYPES),
    )

    op.add_column(
        "whatsapp_inbound_events",
        sa.Column("phone_number_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "whatsapp_inbound_events",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_attempts_non_negative",
        "whatsapp_inbound_events",
        "attempts >= 0",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_max_attempts_positive",
        "whatsapp_inbound_events",
        "max_attempts > 0",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_attempts_bounded",
        "whatsapp_inbound_events",
        "attempts <= max_attempts",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_status_valid",
        "whatsapp_inbound_events",
        "status IN ('received', 'processing', 'domain_processed', "
        "'completed', 'failed', 'dead_letter')",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_completed_requires_timestamp",
        "whatsapp_inbound_events",
        "(status != 'completed') OR (completed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_dead_letter_requires_timestamp",
        "whatsapp_inbound_events",
        "(status != 'dead_letter') OR (dead_lettered_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM notification_outbox "
        "WHERE event_type = 'whatsapp_inbound_response') THEN "
        "RAISE EXCEPTION '0013 downgrade blocked: whatsapp_inbound_response "
        "notifications exist and cannot be represented in the 0012 schema'; "
        "END IF; END $$"
    )

    op.drop_constraint(
        "ck_whatsapp_inbound_dead_letter_requires_timestamp",
        "whatsapp_inbound_events",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_completed_requires_timestamp",
        "whatsapp_inbound_events",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_status_valid",
        "whatsapp_inbound_events",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_attempts_bounded",
        "whatsapp_inbound_events",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_max_attempts_positive",
        "whatsapp_inbound_events",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_attempts_non_negative",
        "whatsapp_inbound_events",
    )
    op.drop_column("whatsapp_inbound_events", "dead_lettered_at")
    op.drop_column("whatsapp_inbound_events", "phone_number_id")

    # Restore original notification_outbox.event_type
    op.drop_constraint("notification_event_type", "notification_outbox")
    op.alter_column(
        "notification_outbox",
        "event_type",
        type_=_notif_enum(*_OLD_EVENT_TYPES),
    )
