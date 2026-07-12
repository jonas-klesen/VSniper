"""drop dirty_since from taste_state

Revision ID: a1b2c3d4e5f6
Revises: 99296b4cc4ff
Create Date: 2026-06-10 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "99296b4cc4ff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("taste_state", "dirty_since")


def downgrade() -> None:
    op.add_column("taste_state", sa.Column("dirty_since", sa.DateTime(timezone=True), nullable=True))
