"""add durable feedback assets

Revision ID: 20260630a003
Revises: 20260630a002
Create Date: 2026-06-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260630a003"
down_revision: Union[str, Sequence[str], None] = "20260630a002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("taste_samples", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stored_image_paths", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("taste_samples", schema=None) as batch_op:
        batch_op.drop_column("stored_image_paths")
