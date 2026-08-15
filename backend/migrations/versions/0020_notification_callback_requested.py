"""Add the ``callback_requested`` notification event type.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-15

#36 persists a CALLBACK pending action when a voice caller can't finish booking;
#41 surfaces it to the owner. Part A of #41 pushes an owner WhatsApp notification
when that callback is persisted, which needs a new ``notification_outbox``
event_type value, ``callback_requested``.

Like ``pending_actions.action_type``, ``notification_outbox.event_type`` is a
VARCHAR with a named CHECK constraint (``enum_type(..., native_enum=False)``),
NOT a native PostgreSQL enum — so there is no ``ALTER TYPE ADD VALUE``
transaction gotcha. Adding a value is dropping and recreating the CHECK, which
is transaction safe. This is the exact pattern migration 0013 used to add
``whatsapp_inbound_response`` (drop_constraint -> alter_column with a widened
Enum -> fail-closed downgrade guard -> restore the old Enum).

The allowed value set is DERIVED from ``NotificationEventType`` in code below, so
the constraint can never drift from the enum it mirrors.
"""

from alembic import op
from sqlalchemy import Enum as SAEnum
from sqlalchemy import text

from fonely.models.enums import NotificationEventType

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_CONSTRAINT = "notification_event_type"
_TABLE = "notification_outbox"
_COLUMN = "event_type"

# Derived from the enum so the constraint mirrors the model exactly — never a
# hand-typed value list that could drift from NotificationEventType.
_NEW_VALUES = tuple(member.value for member in NotificationEventType)
_OLD_VALUES = tuple(
    member.value
    for member in NotificationEventType
    if member is not NotificationEventType.CALLBACK_REQUESTED
)


def _notif_enum(*values: str) -> SAEnum:
    return SAEnum(*values, name=_CONSTRAINT, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE)
    op.alter_column(_TABLE, _COLUMN, type_=_notif_enum(*_NEW_VALUES))


def downgrade() -> None:
    # Fail closed: restoring the pre-0020 constraint (without callback_requested)
    # while such notifications exist would reject rows carrying a caller's phone +
    # booking intent. Refuse and say so, mirroring 0013's guard and 0019's.
    op.execute(
        text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_TABLE} "
            f"WHERE {_COLUMN} = 'callback_requested') THEN "
            "RAISE EXCEPTION '0020 downgrade blocked: callback_requested "
            "notifications exist and cannot be represented in the 0019 schema. "
            "Deliver or delete them, then downgrade.'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(_CONSTRAINT, _TABLE)
    op.alter_column(_TABLE, _COLUMN, type_=_notif_enum(*_OLD_VALUES))
