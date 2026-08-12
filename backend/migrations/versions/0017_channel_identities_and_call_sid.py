"""Provider channel identity in the database, and a correlatable call SID.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-12

Two defects on the voice path, both of which make a real patient call unsafe
or unroutable, and both of which are fixed here in the schema rather than in
application code.

1. Which clinic owns a dialed number lived in EXOTEL_NUMBER_MAPPINGS, a
   required environment variable. Attaching a clinic's signboard number meant
   a redeploy, and a JSON typo silently produced an empty map, which turns
   every inbound call into "unknown number". This is the same defect 0016
   fixed for WhatsApp, on the channel a patient actually dials.

   The table here is deliberately generic — (provider, external_identifier) —
   rather than an exotel_numbers table. The identifier a provider hands us is
   the only tenant-bearing fact on an inbound call, and we already know we
   will have more than one provider. business_whatsapp_channels is NOT folded
   into it in this migration: those rows are live and proven, and moving them
   is a data migration whose failure mode is silently unrouting a working
   clinic. That belongs in its own change, not underneath the voice work.

2. calls had no provider-side identifier, so the completion handler correlated
   by "the most recent call from this phone number with no ended_at". A caller
   who redials while the first leg is still open closes the wrong row, and a
   retried webhook double-counts. Storing the provider's own call id makes
   correlation exact.

   The unique index on it is what makes the ringing webhook idempotent: the
   provider retries on timeout and may deliver out of order, so INSERT must be
   able to say ON CONFLICT DO NOTHING and still end up with exactly one row.

   call_provider and provider_call_sid are constrained to be both NULL or both
   set. Without that, a NULL provider beside a non-NULL sid would sit outside
   the unique index — Postgres treats NULLs as distinct — and the idempotency
   guarantee would quietly not hold for exactly the rows that need it. Calls
   from the browser demo have neither and stay outside the index by design.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import text

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    op.create_table(
        "business_channel_identities",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "business_id",
            sa.Integer,
            sa.ForeignKey("businesses.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("external_identifier", sa.String(100), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
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
            name="ck_business_channel_identities_status",
        ),
        sa.CheckConstraint(
            "length(external_identifier) > 0",
            name="ck_business_channel_identities_identifier_nonempty",
        ),
        sa.CheckConstraint(
            "length(provider) > 0",
            name="ck_business_channel_identities_provider_nonempty",
        ),
    )

    # One dialed number belongs to exactly one tenant. An inbound call carries
    # the number and nothing else that identifies the clinic, so if two
    # businesses could claim it the resolver would have to pick, and would
    # sometimes route a patient into another clinic's records.
    op.create_unique_constraint(
        "uq_business_channel_identities_provider_identifier",
        "business_channel_identities",
        ["provider", "external_identifier"],
    )

    # At most one active primary per (business, provider), so "the number this
    # clinic is reached on for this provider" is deterministic rather than
    # decided by row order.
    op.create_index(
        "uq_business_channel_identities_one_active_primary",
        "business_channel_identities",
        ["business_id", "provider"],
        unique=True,
        postgresql_where=text("is_primary AND status = 'active'"),
    )

    op.create_index(
        "ix_business_channel_identities_business_active",
        "business_channel_identities",
        ["business_id", "status"],
    )

    op.add_column("calls", sa.Column("call_provider", sa.String(30), nullable=True))
    op.add_column("calls", sa.Column("provider_call_sid", sa.String(100), nullable=True))

    op.create_check_constraint(
        "ck_calls_provider_sid_paired",
        "calls",
        "(call_provider IS NULL) = (provider_call_sid IS NULL)",
    )

    # Idempotent ringing: a retried or out-of-order webhook for a call we have
    # already recorded conflicts here instead of creating a second call row.
    op.create_index(
        "uq_calls_provider_call_sid",
        "calls",
        ["call_provider", "provider_call_sid"],
        unique=True,
        postgresql_where=text("provider_call_sid IS NOT NULL"),
    )


def downgrade() -> None:
    # Same reasoning as 0016: these rows are the only record of which dialed
    # number reaches which clinic, and nothing else in the schema can rebuild
    # them. Refuse rather than silently discard routing an operator would have
    # to reconstruct by hand while the phone is ringing.
    if not context.is_offline_mode():
        conn = op.get_bind()
        # Lock before counting so a registration cannot land between the guard
        # and the drop and be destroyed without ever being counted.
        conn.execute(text("LOCK TABLE business_channel_identities IN ACCESS EXCLUSIVE MODE"))
        count = conn.execute(text("SELECT count(*) FROM business_channel_identities")).scalar()
        if count:
            raise RuntimeError(
                f"refusing lossy downgrade: business_channel_identities holds {count} "
                "row(s) of tenant routing configuration with no other source of "
                "truth. Export them, then delete them explicitly, then downgrade."
            )

    op.drop_index("uq_calls_provider_call_sid", table_name="calls")
    op.drop_constraint("ck_calls_provider_sid_paired", "calls", type_="check")
    op.drop_column("calls", "provider_call_sid")
    op.drop_column("calls", "call_provider")

    op.drop_index(
        "ix_business_channel_identities_business_active",
        table_name="business_channel_identities",
    )
    op.drop_index(
        "uq_business_channel_identities_one_active_primary",
        table_name="business_channel_identities",
    )
    op.drop_table("business_channel_identities")
