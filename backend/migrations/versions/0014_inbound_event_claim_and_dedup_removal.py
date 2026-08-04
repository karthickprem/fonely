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


def _notification_status_enum(*values: str) -> sa.Enum:
    return sa.Enum(
        *values,
        name="notification_status",
        native_enum=False,
        create_constraint=True,
    )


def upgrade() -> None:
    op.add_column(
        "notification_outbox",
        sa.Column("claim_token", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "notification_outbox",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notification_outbox",
        sa.Column("claim_version", sa.Integer(), nullable=False, server_default="1"),
    )
    # Pre-0014 workers could leave processing rows without durable ownership.
    # Return them to failed so the leased worker can reclaim them safely.
    op.execute(
        "UPDATE notification_outbox SET status='failed', "
        "next_attempt_at=NOW(), last_error='migration_recovered_processing' "
        "WHERE status='processing'"
    )
    op.create_check_constraint(
        "ck_notification_claim_consistency",
        "notification_outbox",
        "(status = 'processing' AND claim_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) "
        "OR (status != 'processing' AND claim_token IS NULL "
        "AND lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_notification_claim_version_positive",
        "notification_outbox",
        "claim_version > 0",
    )

    op.drop_constraint("notification_status", "notification_outbox")
    op.alter_column(
        "notification_outbox",
        "status",
        type_=_notification_status_enum(
            "pending",
            "processing",
            "delivered",
            "failed",
            "dead_letter",
            "unknown",
        ),
    )
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
    op.execute(
        "UPDATE whatsapp_inbound_events "
        "SET provider_timestamp = created_at "
        "WHERE provider_timestamp IS NULL"
    )
    op.alter_column(
        "whatsapp_inbound_events",
        "provider_timestamp",
        nullable=False,
    )
    op.execute(
        "UPDATE whatsapp_inbound_events "
        "SET phone_number_id = 'legacy-unknown' "
        "WHERE phone_number_id IS NULL OR phone_number_id = ''"
    )
    op.alter_column(
        "whatsapp_inbound_events",
        "phone_number_id",
        nullable=False,
    )
    op.execute(
        "UPDATE whatsapp_inbound_events SET status='failed', "
        "next_attempt_at=NOW(), last_error='migration_recovered_processing' "
        "WHERE status='processing'"
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
        "length(phone_number_id) > 0",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_claim_consistency",
        "whatsapp_inbound_events",
        "(status = 'processing' AND claim_token IS NOT NULL "
        "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) "
        "OR (status != 'processing' AND claim_token IS NULL "
        "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_claim_version_positive",
        "whatsapp_inbound_events",
        "claim_version > 0",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_completed_requires_timestamp",
        "whatsapp_inbound_events",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_completed_requires_timestamp",
        "whatsapp_inbound_events",
        "(status != 'completed') OR (completed_at IS NOT NULL AND message_body IS NULL)",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_dead_letter_requires_timestamp",
        "whatsapp_inbound_events",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_dead_letter_requires_timestamp",
        "whatsapp_inbound_events",
        "(status NOT IN ('dead_letter', 'response_failed')) OR (dead_lettered_at IS NOT NULL)",
    )

    op.create_table(
        "whatsapp_delivery_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "business_id",
            sa.Integer(),
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column(
            "notification_event_id",
            sa.Integer(),
            sa.ForeignKey("notification_outbox.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("error_class", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "notification_event_id",
            "attempt_number",
            name="uq_whatsapp_delivery_attempt",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'accepted', 'delivered', 'failed', 'unknown')",
            name="ck_whatsapp_delivery_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_whatsapp_delivery_attempt_number",
        ),
    )
    op.create_index(
        "ix_whatsapp_delivery_attempt_provider_message",
        "whatsapp_delivery_attempts",
        ["provider_message_id"],
        unique=False,
    )

    # Preserve historical dedup evidence as terminal inbox tombstones before
    # removing the obsolete second source of truth.
    op.execute(
        "INSERT INTO whatsapp_inbound_events "
        "(message_id, business_id, phone_number_id, sender_phone, message_type, "
        "message_body, status, attempts, max_attempts, provider_timestamp, "
        "created_at, completed_at) "
        "SELECT message_id, business_id, 'legacy-unknown', 'legacy-unknown', "
        "'legacy_processed', NULL, 'completed', 0, 5, processed_at, "
        "processed_at, processed_at FROM whatsapp_processed_messages "
        "ON CONFLICT (message_id) DO NOTHING"
    )
    op.drop_table("whatsapp_processed_messages")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM notification_outbox WHERE status = 'unknown') THEN "
        "RAISE EXCEPTION '0014 downgrade blocked: reconcile unknown notification rows first'; "
        "END IF; "
        "IF EXISTS (SELECT 1 FROM whatsapp_inbound_events "
        "WHERE status = 'response_failed') THEN "
        "RAISE EXCEPTION '0014 downgrade blocked: repair response_failed inbound rows first'; "
        "END IF; END $$"
    )

    op.drop_constraint("notification_status", "notification_outbox")
    op.alter_column(
        "notification_outbox",
        "status",
        type_=_notification_status_enum(
            "pending",
            "processing",
            "delivered",
            "failed",
            "dead_letter",
        ),
    )
    op.drop_constraint(
        "ck_notification_claim_version_positive",
        "notification_outbox",
    )
    op.drop_constraint(
        "ck_notification_claim_consistency",
        "notification_outbox",
    )
    op.drop_column("notification_outbox", "claim_version")
    op.drop_column("notification_outbox", "lease_expires_at")
    op.drop_column("notification_outbox", "claim_token")

    op.drop_constraint(
        "ck_whatsapp_inbound_dead_letter_requires_timestamp",
        "whatsapp_inbound_events",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_dead_letter_requires_timestamp",
        "whatsapp_inbound_events",
        "(status != 'dead_letter') OR (dead_lettered_at IS NOT NULL)",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_completed_requires_timestamp",
        "whatsapp_inbound_events",
    )
    op.create_check_constraint(
        "ck_whatsapp_inbound_completed_requires_timestamp",
        "whatsapp_inbound_events",
        "(status != 'completed') OR (completed_at IS NOT NULL)",
    )
    op.drop_constraint(
        "ck_whatsapp_inbound_claim_version_positive",
        "whatsapp_inbound_events",
    )

    op.drop_index(
        "ix_whatsapp_delivery_attempt_provider_message",
        table_name="whatsapp_delivery_attempts",
    )
    op.drop_table("whatsapp_delivery_attempts")

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
    op.execute(
        "INSERT INTO whatsapp_processed_messages "
        "(message_id, business_id, processed_at) "
        "SELECT message_id, business_id, COALESCE(completed_at, created_at) "
        "FROM whatsapp_inbound_events ON CONFLICT (message_id) DO NOTHING"
    )

    op.drop_constraint(
        "ck_whatsapp_inbound_claim_consistency",
        "whatsapp_inbound_events",
    )

    # Remove phone_number_id nonempty constraint
    op.drop_constraint(
        "ck_whatsapp_inbound_phone_number_id_nonempty",
        "whatsapp_inbound_events",
    )
    op.alter_column(
        "whatsapp_inbound_events",
        "phone_number_id",
        nullable=True,
    )
    op.alter_column(
        "whatsapp_inbound_events",
        "provider_timestamp",
        nullable=True,
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
