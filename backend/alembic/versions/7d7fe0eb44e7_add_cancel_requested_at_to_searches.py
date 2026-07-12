"""add cancel_requested_at to searches

Revision ID: 7d7fe0eb44e7
Revises: 6e477f2f0083
Create Date: 2026-07-02 18:54:38.187244

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d7fe0eb44e7'
down_revision: Union[str, Sequence[str], None] = '6e477f2f0083'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('searches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('searches', schema=None) as batch_op:
        batch_op.drop_column('cancel_requested_at')
