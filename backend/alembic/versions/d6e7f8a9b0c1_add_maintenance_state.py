"""add maintenance state

Revision ID: d6e7f8a9b0c1
Revises: b1c2d3e4f5a6
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "maintenance_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="idle"),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("owner", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "worker_activity",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worker_activity_heartbeat", "worker_activity", ["heartbeat_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_worker_activity_heartbeat", table_name="worker_activity")
    op.drop_table("worker_activity")
    op.drop_table("maintenance_state")
