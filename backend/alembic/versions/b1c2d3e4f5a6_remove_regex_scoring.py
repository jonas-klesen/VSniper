"""remove regex scoring

Revision ID: b1c2d3e4f5a6
Revises: f7e5959f917c
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "f7e5959f917c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM candidates WHERE grading_stage = 'regex_rejected'")
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("vlm_judge_parallel_requests", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.drop_column("regex_min_score_for_vlm")
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.drop_column("lexical_score")


def downgrade() -> None:
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lexical_score", sa.Float(), nullable=False, server_default="0"))
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("regex_min_score_for_vlm", sa.Float(), nullable=False, server_default="3.0"))
        batch_op.drop_column("vlm_judge_parallel_requests")
