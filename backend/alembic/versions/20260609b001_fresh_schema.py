"""fresh schema

Revision ID: 20260609b001
Revises:
Create Date: 2026-06-09 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260609b001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ai_usage_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ai_usage_events_called_at"), ["called_at"], unique=False)

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vinted_region", sa.String(length=16), nullable=False),
        sa.Column("vinted_cookie", sa.Text(), nullable=False),
        sa.Column("vinted_refresh_token", sa.Text(), nullable=False),
        sa.Column("telegram_bot_token", sa.Text(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=False),
        sa.Column("telegram_webhook_url", sa.Text(), nullable=False),
        sa.Column("telegram_webhook_secret", sa.Text(), nullable=False),
        sa.Column("telegram_configured", sa.Boolean(), nullable=False),
        sa.Column("ai_judge_provider", sa.String(length=64), nullable=False),
        sa.Column("ai_judge_model", sa.String(length=128), nullable=False),
        sa.Column("local_judge_model", sa.String(length=128), nullable=False),
        sa.Column("ai_judge_allow_openai_fallback", sa.Boolean(), nullable=False),
        sa.Column("ai_judge_reasoning_effort", sa.String(length=32), nullable=False),
        sa.Column("local_vlm_base_url", sa.String(length=512), nullable=False),
        sa.Column("ai_learn_model", sa.String(length=128), nullable=False),
        sa.Column("ai_learn_reasoning_effort", sa.String(length=32), nullable=False),
        sa.Column("ai_observation_provider", sa.String(length=64), nullable=False),
        sa.Column("local_observation_model", sa.String(length=128), nullable=False),
        sa.Column("regex_min_score_for_vlm", sa.Float(), nullable=False),
        sa.Column("vlm_grid_size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("session_health", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "learning_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_weights", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("old_prompt", sa.Text(), nullable=True),
        sa.Column("new_prompt", sa.Text(), nullable=True),
        sa.Column("source_counts", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "preference_profiles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("images", sa.JSON(), nullable=False),
        sa.Column("active_prompt", sa.Text(), nullable=False),
        sa.Column("taste_profile", sa.JSON(), nullable=False),
        sa.Column("reference_observations", sa.JSON(), nullable=False),
        sa.Column("extracted_attributes", sa.JSON(), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "searches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("clothing_item", sa.String(length=32), nullable=False),
        sa.Column("query", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_status", sa.String(length=16), nullable=False),
        sa.Column("last_found_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clothing_item", name="uq_searches_clothing_item"),
    )
    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_searches_clothing_item"), ["clothing_item"], unique=False)

    op.create_table(
        "taste_samples",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("clothing_item", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("vinted_url", sa.Text(), nullable=True),
        sa.Column("external_item_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("brand", sa.String(length=255), nullable=False),
        sa.Column("price_eur", sa.Float(), nullable=True),
        sa.Column("size", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("image_urls", sa.JSON(), nullable=False),
        sa.Column("cached_image_paths", sa.JSON(), nullable=False),
        sa.Column("image_observations", sa.JSON(), nullable=False),
        sa.Column("normalized_listing", sa.JSON(), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("taste_samples", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_taste_samples_candidate_id"), ["candidate_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_taste_samples_clothing_item"), ["clothing_item"], unique=False)
        batch_op.create_index(batch_op.f("ix_taste_samples_external_item_id"), ["external_item_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_taste_samples_kind"), ["kind"], unique=False)

    op.create_table(
        "taste_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("manual_note", sa.Text(), nullable=False),
        sa.Column("manual_note_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("taste_profile", sa.JSON(), nullable=False),
        sa.Column("reference_observations", sa.JSON(), nullable=False),
        sa.Column("last_recomputed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dirty_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recompute_status", sa.String(length=16), nullable=False),
        sa.Column("recompute_job_id", sa.String(length=64), nullable=True),
        sa.Column("recompute_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recompute_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recompute_error", sa.Text(), nullable=True),
        sa.Column("last_recompute_cost_usd", sa.Float(), nullable=False),
        sa.Column("last_recompute_input_tokens", sa.Integer(), nullable=False),
        sa.Column("last_recompute_output_tokens", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "candidates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("external_item_id", sa.String(length=128), nullable=True),
        sa.Column("clothing_item", sa.String(length=32), nullable=False),
        sa.Column("search_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("brand", sa.String(length=255), nullable=False),
        sa.Column("price_eur", sa.Float(), nullable=False),
        sa.Column("size", sa.String(length=32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("image_urls", sa.JSON(), nullable=False),
        sa.Column("source_region", sa.String(length=16), nullable=True),
        sa.Column("matched_filters", sa.JSON(), nullable=False),
        sa.Column("matched_preferences", sa.JSON(), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("normalized_listing", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_mode", sa.String(length=16), nullable=True),
        sa.Column("extraction_status", sa.String(length=32), nullable=False),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column("score_trace", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=False),
        sa.Column("ai_observation", sa.JSON(), nullable=False),
        sa.Column("lexical_score", sa.Float(), nullable=False),
        sa.Column("grading_stage", sa.String(length=32), nullable=False),
        sa.Column("feedback", sa.String(length=32), nullable=False),
        sa.Column("feedback_comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["search_id"], ["searches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_candidates_clothing_item"), ["clothing_item"], unique=False)
        batch_op.create_index("ix_candidates_decision", ["decision"], unique=False)
        batch_op.create_index(batch_op.f("ix_candidates_external_item_id"), ["external_item_id"], unique=False)
        batch_op.create_index("ix_candidates_search_extraction", ["search_id", "extraction_status"], unique=False)

    op.create_table(
        "alert_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_preview", sa.Text(), nullable=True),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("alert_deliveries", schema=None) as batch_op:
        batch_op.create_index(
            "ix_alert_deliveries_active_candidate_channel",
            ["candidate_id", "channel"],
            unique=True,
            sqlite_where=sa.text("status IN ('pending', 'processing', 'sent')"),
        )
        batch_op.create_index(batch_op.f("ix_alert_deliveries_candidate_id"), ["candidate_id"], unique=False)
        batch_op.create_index("ix_alert_deliveries_status", ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("alert_deliveries", schema=None) as batch_op:
        batch_op.drop_index("ix_alert_deliveries_status")
        batch_op.drop_index(batch_op.f("ix_alert_deliveries_candidate_id"))
        batch_op.drop_index(
            "ix_alert_deliveries_active_candidate_channel",
            sqlite_where=sa.text("status IN ('pending', 'processing', 'sent')"),
        )
    op.drop_table("alert_deliveries")

    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.drop_index("ix_candidates_search_extraction")
        batch_op.drop_index(batch_op.f("ix_candidates_external_item_id"))
        batch_op.drop_index("ix_candidates_decision")
        batch_op.drop_index(batch_op.f("ix_candidates_clothing_item"))
    op.drop_table("candidates")

    op.drop_table("taste_state")

    with op.batch_alter_table("taste_samples", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_taste_samples_kind"))
        batch_op.drop_index(batch_op.f("ix_taste_samples_external_item_id"))
        batch_op.drop_index(batch_op.f("ix_taste_samples_clothing_item"))
        batch_op.drop_index(batch_op.f("ix_taste_samples_candidate_id"))
    op.drop_table("taste_samples")

    with op.batch_alter_table("searches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_searches_clothing_item"))
    op.drop_table("searches")

    op.drop_table("preference_profiles")
    op.drop_table("learning_snapshots")
    op.drop_table("app_settings")

    with op.batch_alter_table("ai_usage_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ai_usage_events_called_at"))
    op.drop_table("ai_usage_events")
