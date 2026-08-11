"""Move WhatsApp channel identity from process config into the database.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-11

Before this migration the phone_number_id <-> business_id mapping lived in the
WHATSAPP_BUSINESS_MAPPINGS environment variable. That made tenant identity a
property of a process rather than of the tenant: onboarding a clinic required
a deploy, and appointment commit failed closed for any business absent from
the variable.

Two invariants are enforced here in the database rather than in application
code, because both are tenant-isolation properties and application code is the
thing most likely to be wrong:

  1. phone_number_id is globally unique. An inbound webhook carries a
     phone_number_id and nothing else that identifies the tenant, so if two
     businesses could claim the same value the resolver would have to pick one
     and would sometimes route a patient's message into another clinic's
     records. Uniqueness makes that unrepresentable.

  2. At most one active primary channel per business. Outbound selection must
     be deterministic; without this, a clinic with two numbers could send from
     an arbitrary one depending on row order.

The table is created empty. That is deliberate: an empty table reproduces
exactly today's behaviour for an unconfigured business (commit refuses with
whatsapp_mapping_missing) rather than inventing a mapping that no one
authorized. Backfill is an explicit operator step.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import text

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    op.create_table(
        "business_whatsapp_channels",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "business_id",
            sa.Integer,
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column("phone_number_id", sa.String(100), nullable=False),
        sa.Column("waba_id", sa.String(100), nullable=True),
        sa.Column("display_phone_number", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "is_primary",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_business_whatsapp_channels_status",
        ),
        sa.CheckConstraint(
            "length(phone_number_id) > 0",
            name="ck_business_whatsapp_channels_pnid_nonempty",
        ),
    )

    # Invariant 1: one provider number belongs to exactly one tenant, forever.
    op.create_unique_constraint(
        "uq_business_whatsapp_channels_phone_number_id",
        "business_whatsapp_channels",
        ["phone_number_id"],
    )

    # Invariant 2: deterministic outbound sender selection. Declared through
    # create_index rather than raw SQL so the ORM/migration parity check can
    # actually see it — a raw execute() is opaque to that comparison and the
    # two definitions could drift apart unnoticed.
    op.create_index(
        "uq_business_whatsapp_channels_one_active_primary",
        "business_whatsapp_channels",
        ["business_id"],
        unique=True,
        postgresql_where=text("is_primary AND status = 'active'"),
    )

    op.create_index(
        "ix_business_whatsapp_channels_business_active",
        "business_whatsapp_channels",
        ["business_id", "status"],
    )


def downgrade() -> None:
    # Downgrade is lossy: these rows are the only record of which provider
    # number belongs to which tenant, and they cannot be reconstructed from
    # anything else in the schema. Refuse rather than silently discard tenant
    # routing configuration that an operator would have to rebuild by hand.
    if not context.is_offline_mode():
        conn = op.get_bind()
        # Take the lock before counting, so a concurrent registration cannot
        # land between the guard and the drop and be silently destroyed.
        conn.execute(text("LOCK TABLE business_whatsapp_channels IN ACCESS EXCLUSIVE MODE"))
        count = conn.execute(text("SELECT count(*) FROM business_whatsapp_channels")).scalar()
        if count:
            raise RuntimeError(
                f"refusing lossy downgrade: business_whatsapp_channels holds {count} "
                "row(s) of tenant routing configuration with no other source of "
                "truth. Export them, then delete them explicitly, then downgrade."
            )

    op.drop_index(
        "ix_business_whatsapp_channels_business_active",
        table_name="business_whatsapp_channels",
    )
    op.drop_index(
        "uq_business_whatsapp_channels_one_active_primary",
        table_name="business_whatsapp_channels",
    )
    op.drop_table("business_whatsapp_channels")
