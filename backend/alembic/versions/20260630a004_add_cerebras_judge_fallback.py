"""add cerebras judge fallback settings

Revision ID: 20260630a004
Revises: 20260630a003
Create Date: 2026-06-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260630a004"
down_revision: Union[str, Sequence[str], None] = "20260630a003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ai_judge_fallback_provider", sa.String(length=32), nullable=False, server_default="none"))
        batch_op.add_column(sa.Column("cerebras_judge_model", sa.String(length=128), nullable=False, server_default="gemma-4-31b"))

    op.execute(
        "UPDATE app_settings SET ai_judge_fallback_provider = 'openai' "
        "WHERE ai_judge_allow_openai_fallback = 1"
    )


def downgrade() -> None:
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_column("cerebras_judge_model")
        batch_op.drop_column("ai_judge_fallback_provider")
