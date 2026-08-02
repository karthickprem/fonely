"""business_onboarding

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-02

Adds business onboarding draft and configuration commit tables for
persisting approved onboarding configurations and activating them
into the operational schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AFFECTED_TABLES = (
    "business_configuration_commits",
    "business_onboarding_drafts",
    "business_users",
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
        "business_onboarding_drafts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "status",
            _enum(
                ("draft", "pending_review", "approved", "activated", "rejected"),
                "onboarding_draft_status",
            ),
            nullable=False,
        ),
        sa.Column("draft_data", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("draft_digest", sa.String(64), nullable=False),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("business_id", "draft_digest", name="uq_onboarding_draft_digest"),
        sa.CheckConstraint("version > 0", name="ck_onboarding_draft_version"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["submitted_by_user_id"], ["business_users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["business_users.id"]),
    )

    op.create_table(
        "business_configuration_commits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("onboarding_draft_id", sa.Integer(), nullable=False),
        sa.Column("draft_digest", sa.String(64), nullable=False),
        sa.Column("committed_by_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("commit_evidence", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("rollback_evidence", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["onboarding_draft_id"], ["business_onboarding_drafts.id"]),
        sa.ForeignKeyConstraint(["committed_by_user_id"], ["business_users.id"]),
    )


def downgrade() -> None:
    _lock_affected_tables()
    op.drop_table("business_configuration_commits")
    op.drop_table("business_onboarding_drafts")
