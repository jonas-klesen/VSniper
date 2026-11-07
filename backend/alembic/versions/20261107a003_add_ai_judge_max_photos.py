"""add ai judge max photos

The judge sends up to this many photos of a listing, each as its own image input.

Revision ID: 20261107a003
Revises: 20261107a002
Create Date: 2026-11-07 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20261107a003'
down_revision: Union[str, Sequence[str], None] = '20261107a002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ai_judge_max_photos', sa.Integer(), nullable=False, server_default='6'))


def downgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('ai_judge_max_photos')
