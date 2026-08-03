"""conversation_turn_unique

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-03

Adds unique constraint on (conversation_id, turn_number) to prevent
duplicate turns from concurrent requests.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_conversation_turns_conv_turn",
        "conversation_turns",
        ["conversation_id", "turn_number"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_conversation_turns_conv_turn",
        "conversation_turns",
        type_="unique",
    )
