"""verify model for shortlisted listings

A hosted model re-judges listings the local judge shortlists; its score decides the alert.

Revision ID: 20261108a001
Revises: 20261107a004
Create Date: 2026-11-08 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20261108a001'
down_revision: Union[str, Sequence[str], None] = '20261107a004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('verify_model_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('verify_alert_threshold', sa.Integer(), nullable=False, server_default='80'))


def downgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('verify_alert_threshold')
        batch_op.drop_column('verify_model_id')
