"""Add retention-independent notification manifest table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-10

One row per committed appointment operation (create/cancel/reschedule).
Survives outbox retention cleanup. Written atomically with appointment
mutation inside the caller's transaction.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    op.create_table(
        "notification_manifests",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("business_id", sa.Integer, sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.Integer, nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("pending_action_id", sa.Integer, nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_phone", sa.String(20), nullable=True),
        sa.Column("actor_bu_id", sa.Integer, nullable=True),
        sa.Column("recipient_count", sa.Integer, nullable=False),
        sa.Column("recipient_manifest", JSONB, nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("phone_number_id", sa.String(100), nullable=False),
        sa.Column("equivalence_digest", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("outbox_event_ids", ARRAY(sa.Integer), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "operation IN ('create', 'cancel', 'reschedule')",
            name="ck_manifest_operation",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_manifest_schema_version",
        ),
        sa.CheckConstraint(
            "recipient_count > 0",
            name="ck_manifest_recipient_count",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(recipient_manifest) = 'array' "
            "AND jsonb_array_length(recipient_manifest) > 0",
            name="ck_manifest_recipient_array",
        ),
        sa.CheckConstraint(
            "octet_length(recipient_manifest::text) <= 102400",
            name="ck_manifest_size",
        ),
        sa.CheckConstraint(
            "(actor_kind = 'system' AND actor_phone IS NULL AND actor_bu_id IS NULL) "
            "OR (actor_kind = 'customer' AND actor_phone IS NOT NULL AND actor_bu_id IS NULL) "
            "OR (actor_kind = 'owner' AND actor_phone IS NOT NULL AND actor_bu_id IS NOT NULL) "
            "OR (actor_kind = 'manager' AND actor_phone IS NOT NULL AND actor_bu_id IS NOT NULL)",
            name="ck_manifest_actor_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "pending_action_id"],
            ["pending_actions.business_id", "pending_actions.id"],
            name="fk_manifest_pending_action",
            ondelete="RESTRICT",
        ),
    )

    op.create_index(
        "uq_manifest_operation_instance",
        "notification_manifests",
        ["business_id", "pending_action_id"],
        unique=True,
    )
    op.create_index(
        "ix_manifest_entity",
        "notification_manifests",
        ["business_id", "entity_type", "entity_id"],
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        conn = op.get_bind()
        conn.execute(text("LOCK TABLE notification_manifests IN ACCESS EXCLUSIVE MODE"))
        count = conn.execute(text("SELECT count(*) FROM notification_manifests")).scalar()
        if count > 0:
            raise RuntimeError(
                f"Cannot downgrade: {count} notification manifest(s) exist. "
                "Resolve retention before downgrading."
            )
    op.drop_index("ix_manifest_entity", table_name="notification_manifests")
    op.drop_index("uq_manifest_operation_instance", table_name="notification_manifests")
    op.drop_table("notification_manifests")
