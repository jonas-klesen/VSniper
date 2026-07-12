"""add search run new judged count

Revision ID: 20260630a001
Revises: 0a1b2c3d4e5f
Create Date: 2026-06-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260630a001"
down_revision: Union[str, Sequence[str], None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("search_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("new_judged_count", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("search_runs", schema=None) as batch_op:
        batch_op.drop_column("new_judged_count")
