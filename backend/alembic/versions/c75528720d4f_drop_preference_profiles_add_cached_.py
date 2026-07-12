"""drop preference_profiles, add cached_input_tokens, index candidates.created_at, dedup failed deliveries

Revision ID: c75528720d4f
Revises: 4777431c41e3
Create Date: 2026-06-09 21:54:15.452669

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'c75528720d4f'
down_revision: Union[str, Sequence[str], None] = '4777431c41e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table('preference_profiles')
    with op.batch_alter_table('ai_usage_events', schema=None) as batch_op:
        # server_default backfills existing rows; the column is non-nullable.
        batch_op.add_column(sa.Column('cached_input_tokens', sa.Integer(), nullable=False, server_default='0'))

    with op.batch_alter_table('candidates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_candidates_created_at'), ['created_at'], unique=False)

    # Recreate the partial unique dedup index to also cover "failed" deliveries, so a candidate
    # whose delivery exhausted its retries is not re-queued (with fresh attempts) on every scan.
    op.drop_index('ix_alert_deliveries_active_candidate_channel', table_name='alert_deliveries')
    op.create_index(
        'ix_alert_deliveries_active_candidate_channel',
        'alert_deliveries',
        ['candidate_id', 'channel'],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'processing', 'sent', 'failed')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_alert_deliveries_active_candidate_channel', table_name='alert_deliveries')
    op.create_index(
        'ix_alert_deliveries_active_candidate_channel',
        'alert_deliveries',
        ['candidate_id', 'channel'],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'processing', 'sent')"),
    )

    with op.batch_alter_table('candidates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_candidates_created_at'))

    with op.batch_alter_table('ai_usage_events', schema=None) as batch_op:
        batch_op.drop_column('cached_input_tokens')

    op.create_table('preference_profiles',
    sa.Column('id', sa.VARCHAR(length=64), nullable=False),
    sa.Column('summary', sa.TEXT(), nullable=False),
    sa.Column('filters', sqlite.JSON(), nullable=False),
    sa.Column('notes', sqlite.JSON(), nullable=False),
    sa.Column('images', sqlite.JSON(), nullable=False),
    sa.Column('active_prompt', sa.TEXT(), nullable=False),
    sa.Column('taste_profile', sqlite.JSON(), nullable=False),
    sa.Column('reference_observations', sqlite.JSON(), nullable=False),
    sa.Column('extracted_attributes', sqlite.JSON(), nullable=False),
    sa.Column('weights', sqlite.JSON(), nullable=False),
    sa.Column('last_refreshed_at', sa.DATETIME(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    # ### end Alembic commands ###
