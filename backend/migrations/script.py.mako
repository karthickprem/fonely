"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


# CHECKLIST: update these version-pinned assertions when adding a migration:
#   1. backend/tests/test_migration_parity.py  — assert heads == {"XXXX"}
#   2. backend/tests/test_migration_parity.py  — revision chain dict entry
#   3. backend/tests/test_schema.py            — assert len(tables) == N
#   4. scripts/tests/test_check_deployment_readiness.py — assert heads == ["XXXX"]
#      ^^^ this one is OUTSIDE backend/tests/ and is easy to miss


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
