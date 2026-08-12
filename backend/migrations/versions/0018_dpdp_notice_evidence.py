"""Per-call proof that the patient was read the DPDP notice.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-12

Under the DPDP Act the clinic must be able to show, for a specific patient on a
specific date, that notice was given before their data was collected. "We have
a consent notice in the product" is not that. The provable fact is per call.

Why columns rather than an event inside ``calls.transcript``:

    The retention policy redacts ``calls.transcript`` at 90 days
    (``RETENTION_CALL_TRANSCRIPTS_DAYS``, default 90) by replacing the whole
    JSONB document, while appointments are retained for 365. Evidence written
    into the transcript would therefore be destroyed on schedule, leaving days
    91 through 365 in which we still hold the patient's booking and no longer
    hold the proof of the notice that permitted collecting it. Evidence that
    expires before the data it justifies is not evidence. Columns survive the
    redaction, and the accompanying PostgreSQL test asserts exactly that so a
    future retention change cannot quietly reintroduce the gap.

    The counter-argument — that a separate record can disagree with the
    transcript — is real but weaker: a disagreement is visible and can be
    investigated, whereas a scheduled deletion is silent.

The four columns are nullable because history exists: every call recorded
before this migration was not accompanied by a stored notice, and back-filling
a value would be inventing consent evidence that was never collected. NULL
across all four means "notice not completed", which is a positive statement —
the runtime keeps speech capture closed until the evidence write succeeds.

``ck_calls_dpdp_notice_all_or_none`` makes partial evidence unrepresentable.
A row with ``completed_at`` and no version proves only that *something* was
played, and in an audit it would read as consent. ``num_nonnulls`` is used
instead of a chain of paired null tests so that no unintended combination of
the four can satisfy the constraint.

``ck_calls_dpdp_notice_digest_hex`` keeps the digest in the one form that can
be recomputed. The digest is a length-prefixed sha256 over (version, locale,
exact spoken text); a truncated or upper-cased value would fail to match a
recomputation at the exact moment the evidence is being questioned, so it is
rejected at write time instead.

No index. Nothing queries these columns in the request path — they are read
per call id during an audit, which the primary key already serves. An index
here would be storage and write cost bought for a query that does not exist.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import text

revision = "0018"
down_revision = "0017"


def upgrade() -> None:
    op.add_column(
        "calls",
        sa.Column("dpdp_notice_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("calls", sa.Column("dpdp_notice_version", sa.String(10), nullable=True))
    op.add_column("calls", sa.Column("dpdp_notice_locale", sa.String(10), nullable=True))
    op.add_column(
        "calls",
        sa.Column("dpdp_notice_content_digest", sa.String(64), nullable=True),
    )

    op.create_check_constraint(
        "ck_calls_dpdp_notice_all_or_none",
        "calls",
        "num_nonnulls(dpdp_notice_completed_at, dpdp_notice_version, "
        "dpdp_notice_locale, dpdp_notice_content_digest) IN (0, 4)",
    )

    op.create_check_constraint(
        "ck_calls_dpdp_notice_digest_hex",
        "calls",
        "dpdp_notice_content_digest IS NULL OR dpdp_notice_content_digest ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    # Dropping these columns destroys the only record that a given patient was
    # given notice. Unlike most schema, it cannot be rebuilt from anywhere else
    # — the transcript that would have carried the same fact is redacted at 90
    # days — and its absence is indistinguishable from a call where no notice
    # was played. Refuse rather than silently convert proven consent into
    # "unknown", which is what a regulator would see afterwards.
    if not context.is_offline_mode():
        conn = op.get_bind()
        # Lock before counting so a call completing its notice between the
        # guard and the drop cannot be destroyed without ever being counted.
        conn.execute(text("LOCK TABLE calls IN ACCESS EXCLUSIVE MODE"))
        count = conn.execute(
            text("SELECT count(*) FROM calls WHERE dpdp_notice_completed_at IS NOT NULL")
        ).scalar()
        if count:
            raise RuntimeError(
                f"refusing lossy downgrade: calls holds {count} row(s) of DPDP "
                "notice evidence with no other source of truth. Export them, "
                "then clear the dpdp_notice_* columns explicitly, then downgrade."
            )

    op.drop_constraint("ck_calls_dpdp_notice_digest_hex", "calls", type_="check")
    op.drop_constraint("ck_calls_dpdp_notice_all_or_none", "calls", type_="check")
    op.drop_column("calls", "dpdp_notice_content_digest")
    op.drop_column("calls", "dpdp_notice_locale")
    op.drop_column("calls", "dpdp_notice_version")
    op.drop_column("calls", "dpdp_notice_completed_at")
