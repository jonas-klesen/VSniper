"""add unified operational error events

Revision ID: 20260728a001
Revises: 20260710a001
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728a001"
down_revision: Union[str, Sequence[str], None] = "20260710a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "error_telegram_notifications_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_table(
        "error_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("exception_type", sa.String(length=255), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("related_entity_type", sa.String(length=64), nullable=True),
        sa.Column("related_entity_id", sa.String(length=128), nullable=True),
        sa.Column(
            "telegram_notification_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_requested",
        ),
        sa.Column("telegram_notification_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telegram_notification_last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_notification_last_error", sa.Text(), nullable=True),
        sa.Column("telegram_notification_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_error_events_occurred_at", "error_events", ["occurred_at"])
    op.create_index("ix_error_events_source", "error_events", ["source"])
    op.create_index("ix_error_events_related_entity_id", "error_events", ["related_entity_id"])
    op.create_index(
        "ix_error_events_telegram_notification_status",
        "error_events",
        ["telegram_notification_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_error_events_telegram_notification_status", table_name="error_events")
    op.drop_index("ix_error_events_related_entity_id", table_name="error_events")
    op.drop_index("ix_error_events_source", table_name="error_events")
    op.drop_index("ix_error_events_occurred_at", table_name="error_events")
    op.drop_table("error_events")
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_column("error_telegram_notifications_enabled")
