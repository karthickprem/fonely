"""committed_entity_linkage

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-01

Ensures each order and appointment can be the authoritative committed result of
at most one PendingAction. Inventory movements remain one-to-many by design.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_order_pending_action",
        "orders",
        ["pending_action_id"],
    )
    op.create_unique_constraint(
        "uq_appt_pending_action",
        "appointments",
        ["pending_action_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_appt_pending_action",
        "appointments",
        type_="unique",
    )
    op.drop_constraint(
        "uq_order_pending_action",
        "orders",
        type_="unique",
    )
