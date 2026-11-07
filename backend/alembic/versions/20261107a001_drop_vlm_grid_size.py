"""drop vlm grid size

Every listing is now judged in its own request, so the grid-size setting is gone.

Revision ID: 20261107a001
Revises: 20260728a001
Create Date: 2026-11-07 12:08:13.893719

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20261107a001'
down_revision: Union[str, Sequence[str], None] = '20260728a001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('vlm_grid_size')


def downgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vlm_grid_size', sa.Integer(), nullable=False, server_default='1'))
