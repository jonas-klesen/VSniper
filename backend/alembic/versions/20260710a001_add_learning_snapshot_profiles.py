"""store before-and-after taste profiles in learning snapshots

Revision ID: 20260710a001
Revises: 9c1d2e3f4a5b
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260710a001"
down_revision: Union[str, Sequence[str], None] = "9c1d2e3f4a5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("learning_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("old_taste_profile", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("new_taste_profile", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("learning_snapshots", schema=None) as batch_op:
        batch_op.drop_column("new_taste_profile")
        batch_op.drop_column("old_taste_profile")
