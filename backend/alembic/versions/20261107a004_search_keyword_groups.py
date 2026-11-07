"""search keyword groups

A saved search holds several keyword groups instead of one query; each group runs as its own
Vinted catalog search.

Revision ID: 20261107a004
Revises: 20261107a003
Create Date: 2026-11-07 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20261107a004'
down_revision: Union[str, Sequence[str], None] = '20261107a003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('searches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('queries', sa.JSON(), nullable=False, server_default='[]'))
    op.execute("UPDATE searches SET queries = json_array(trim(query)) WHERE trim(query) != ''")
    with op.batch_alter_table('searches', schema=None) as batch_op:
        batch_op.drop_column('query')


def downgrade() -> None:
    # Only the first keyword group fits the old single query.
    with op.batch_alter_table('searches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('query', sa.String(length=255), nullable=False, server_default=''))
    op.execute("UPDATE searches SET query = coalesce(json_extract(queries, '$[0]'), '')")
    with op.batch_alter_table('searches', schema=None) as batch_op:
        batch_op.drop_column('queries')
