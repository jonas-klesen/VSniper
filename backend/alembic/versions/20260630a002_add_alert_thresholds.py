"""add alert thresholds

Revision ID: 20260630a002
Revises: 20260630a001
Create Date: 2026-06-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260630a002"
down_revision: Union[str, Sequence[str], None] = "20260630a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("alert_threshold", sa.Integer(), nullable=False, server_default="9"))

    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("alert_threshold", sa.Integer(), nullable=True))

    labels = {
        "schuhe": "Schuhe",
        "hosen": "Hosen",
        "obenrum_warm": "Obenrum Warm",
        "obenrum_mittel": "Obenrum Mittel",
        "obenrum_kalt": "Obenrum Kalt",
        "kopf": "Kopf",
    }
    for clothing_item, label in labels.items():
        op.execute(
            sa.text("UPDATE searches SET name = :label WHERE clothing_item = :clothing_item").bindparams(
                label=label,
                clothing_item=clothing_item,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_column("alert_threshold")

    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_column("alert_threshold")
