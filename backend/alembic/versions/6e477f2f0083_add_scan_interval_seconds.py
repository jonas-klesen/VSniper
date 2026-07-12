"""add scan_interval_seconds

Revision ID: 6e477f2f0083
Revises: 20260630a005
Create Date: 2026-07-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e477f2f0083'
down_revision: Union[str, Sequence[str], None] = '20260630a005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scan_interval_seconds', sa.Integer(), nullable=False, server_default='1800'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('scan_interval_seconds')
