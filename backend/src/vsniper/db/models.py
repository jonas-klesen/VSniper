from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vsniper.core.database import Base


class Search(Base):
    __tablename__ = "searches"
    __table_args__ = (UniqueConstraint("clothing_item", name="uq_searches_clothing_item"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    clothing_item: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(16), nullable=False)
    filters: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    alert_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Outcome of the most recent run: "idle" (never run), "running" (claimed, in flight),
    # "completed" (finished and persisted), or "failed" (raised before persisting).
    run_status: Mapped[str] = mapped_column(String(16), default="idle", nullable=False)
    # Set by request_cancel() while run_status="running"; the running scan polls this and stops
    # cooperatively at the next safe checkpoint. Cleared once the run has wound down.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_found_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Counts from the most recent live run: candidates fetched from Vinted and candidates that
    # reached VLM judging. last_found_count above is the alert count.
    last_fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_judged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    candidates: Mapped[list["Candidate"]] = relationship(back_populates="search")


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    clothing_item: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    search_id: Mapped[str] = mapped_column(ForeignKey("searches.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str] = mapped_column(String(255), nullable=False)
    price_eur: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    image_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_region: Mapped[str | None] = mapped_column(String(16), nullable=True)
    matched_filters: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    matched_preferences: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    features: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    normalized_listing: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scan_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_trace: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    final_score: Mapped[float] = mapped_column(Float, nullable=False)
    ai_observation: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    grading_stage: Mapped[str] = mapped_column(String(32), default="vlm_judged", nullable=False)
    feedback: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    feedback_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    search: Mapped[Search] = relationship(back_populates="candidates")
    alert_deliveries: Mapped[list["AlertDeliveryState"]] = relationship(back_populates="candidate")


class AlertDeliveryState(Base):
    __tablename__ = "alert_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), default="telegram", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    candidate: Mapped[Candidate] = relationship(back_populates="alert_deliveries")


class TasteSampleState(Base):
    __tablename__ = "taste_samples"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    clothing_item: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    vinted_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    brand: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    price_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    cached_image_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    stored_image_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    image_observations: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    normalized_listing: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class TasteState(Base):
    __tablename__ = "taste_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manual_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    manual_note_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    taste_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    reference_observations: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    last_recomputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recompute_status: Mapped[str] = mapped_column(String(16), default="idle", nullable=False)
    recompute_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recompute_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recompute_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recompute_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_recompute_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_recompute_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_recompute_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class LearningSnapshotState(Base):
    __tablename__ = "learning_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    changed_weights: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    old_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Keep the complete before/after profiles for the recompute comparison UI.  The
    # prompt fields above pre-date item-specific profiles and remain for backwards
    # compatible prompt-diff rendering.
    old_taste_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_taste_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_counts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AppSettingsState(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vinted_region: Mapped[str] = mapped_column(String(16), nullable=False)
    vinted_cookie: Mapped[str] = mapped_column(Text, default="", nullable=False)
    vinted_refresh_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    telegram_bot_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    telegram_webhook_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    telegram_webhook_secret: Mapped[str] = mapped_column(Text, default="", nullable=False)
    telegram_configured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_telegram_notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    judge_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    judge_fallback_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    learn_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observation_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vlm_grid_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    vlm_pack_multiple_listing_images: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    vlm_judge_parallel_requests: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ai_judge_image_max_px: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    alert_threshold: Mapped[int] = mapped_column(Integer, default=95, nullable=False)
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
    session_health: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # ISO timestamp of the refresh-token expiry we last warned about. Kept in its own column
    # (not session_health) because session_health is replaced wholesale on every health refresh.
    refresh_token_expiry_warning_sent_for: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Brand titles (as returned by Vinted's own brand catalog) that are hard-excluded from every
    # scan before judging — matched case-insensitively against the candidate's extracted brand.
    blocked_brands: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class ErrorEvent(Base):
    __tablename__ = "error_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    exception_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    related_entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    telegram_notification_status: Mapped[str] = mapped_column(
        String(32), default="not_requested", nullable=False, index=True
    )
    telegram_notification_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    telegram_notification_last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    telegram_notification_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_notification_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AiModelConfig(Base):
    __tablename__ = "ai_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(32), nullable=False)
    local_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AiUsageEvent(Base):
    __tablename__ = "ai_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    search_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class MaintenanceState(Base):
    __tablename__ = "maintenance_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)


class WorkerActivityState(Base):
    __tablename__ = "worker_activity"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    search_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="worker", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    judged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_judged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    alert_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queued_delivery_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failures_by_reason: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    judge_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    judge_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    vinted_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vinted_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    cycle_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    phase: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_cycle_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_cycle_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)


Index(
    "ix_alert_deliveries_active_candidate_channel",
    AlertDeliveryState.candidate_id,
    AlertDeliveryState.channel,
    unique=True,
    sqlite_where=AlertDeliveryState.status.in_(("pending", "processing", "sent", "failed")),
)
Index("ix_alert_deliveries_status", AlertDeliveryState.status)
Index("ix_candidates_decision", Candidate.decision)
Index("ix_candidates_search_extraction", Candidate.search_id, Candidate.extraction_status)
