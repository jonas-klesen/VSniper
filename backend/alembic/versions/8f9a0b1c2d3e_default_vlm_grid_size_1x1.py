"""default VLM grid size to 1x1

Revision ID: 8f9a0b1c2d3e
Revises: a1b2c3d4e5f6
Create Date: 2026-06-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "8f9a0b1c2d3e"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE app_settings SET vlm_grid_size = 1")


def downgrade() -> None:
    op.execute("UPDATE app_settings SET vlm_grid_size = 9")
