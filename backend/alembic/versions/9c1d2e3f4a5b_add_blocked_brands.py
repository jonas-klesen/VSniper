"""add blocked_brands

Revision ID: 9c1d2e3f4a5b
Revises: 20260702a001
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c1d2e3f4a5b'
down_revision: Union[str, Sequence[str], None] = '20260702a001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('blocked_brands', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('blocked_brands')
