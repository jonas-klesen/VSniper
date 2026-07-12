"""add operations tables

Revision ID: e1f2a3b4c5d6
Revises: d6e7f8a9b0c1
Create Date: 2026-06-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "search_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("search_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="worker"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("judged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued_delivery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures_by_reason", sa.JSON(), nullable=True),
        sa.Column("judge_provider", sa.String(length=64), nullable=True),
        sa.Column("judge_model", sa.String(length=128), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("vinted_status", sa.String(length=32), nullable=True),
        sa.Column("vinted_detail", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_search_runs_search_id", "search_runs", ["search_id"])
    op.create_index("ix_search_runs_status", "search_runs", ["status"])
    op.create_index("ix_search_runs_started_at", "search_runs", ["started_at"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("cycle_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("phase", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column("ai_usage_events", sa.Column("search_run_id", sa.Integer(), nullable=True))
    op.create_index("ix_ai_usage_events_search_run_id", "ai_usage_events", ["search_run_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_usage_events_search_run_id", table_name="ai_usage_events")
    op.drop_column("ai_usage_events", "search_run_id")
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_search_runs_started_at", table_name="search_runs")
    op.drop_index("ix_search_runs_status", table_name="search_runs")
    op.drop_index("ix_search_runs_search_id", table_name="search_runs")
    op.drop_table("search_runs")
