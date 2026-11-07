"""drop vlm pack multiple listing images

The judge no longer packs several photos into one image, so the packing toggle is gone.

Revision ID: 20261107a002
Revises: 20261107a001
Create Date: 2026-11-07 13:49:40.396994

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20261107a002'
down_revision: Union[str, Sequence[str], None] = '20261107a001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.drop_column('vlm_pack_multiple_listing_images')


def downgrade() -> None:
    with op.batch_alter_table('app_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('vlm_pack_multiple_listing_images', sa.Boolean(), nullable=False, server_default=sa.true())
        )
