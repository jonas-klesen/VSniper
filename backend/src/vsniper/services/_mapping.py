"""Shared contract <-> SQLAlchemy model converters and small env-derived helpers.

These are module-level functions (not methods) so any service can reuse them without
constructing an AppState. They have no internal state beyond reading Settings."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from vsniper.core.config import get_settings
from vsniper.db.models import (
    AiModelConfig as AiModelConfigRow,
    AppSettingsState,
    Candidate,
    LearningSnapshotState,
    Search,
    TasteSampleState,
    TasteState,
)
from vsniper.domain.contracts import (
    AiModelConfig,
    CandidateRecord,
    CLOTHING_ITEM_LABELS,
    ClothingItem,
    LearningSnapshot,
    SearchRecord,
    SessionHealth,
    SettingsSnapshot,
    TasteDirtyCounts,
    TasteManualNote,
    TasteProfile,
    TasteRecomputeState,
    ReferenceObservation,
    TasteSample,
    TasteSnapshot,
)
from vsniper.integrations.openai.tokenization import text_counts


_CONDITION_KEYS = ("status", "condition", "status_title", "item_status")


def extract_condition(raw_listing: object) -> str:
    """Return the first non-empty condition string from a raw listing dict."""
    if not isinstance(raw_listing, dict):
        return ""
    for key in _CONDITION_KEYS:
        value = raw_listing.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True)
class ResolvedAiModel:
    """Plain snapshot of an `ai_models` row, safe to use after its session closes."""

    id: str
    provider: str
    model_name: str
    reasoning_effort: str
    local_base_url: str | None


def resolve_ai_model(session: Session, model_id: str | None) -> ResolvedAiModel | None:
    """Look up an `ai_models` row by id within the given session. Returns None if `model_id`
    is None or the row no longer exists (e.g. it was deleted out from under a stale reference)."""
    if not model_id:
        return None
    row = session.get(AiModelConfigRow, model_id)
    if row is None:
        return None
    return ResolvedAiModel(
        id=row.id,
        provider=row.provider,
        model_name=row.model_name,
        reasoning_effort=row.reasoning_effort,
        local_base_url=row.local_base_url,
    )


def is_value_configured(value: str, *, placeholder: str) -> bool:
    return bool(value and value.strip() and value != placeholder)


def integration_configuration(
    db_cookie: str | None = None,
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
) -> tuple[bool, bool, bool]:
    """Returns (vinted_configured, telegram_configured, ai_configured).

    If db_cookie is provided, vinted_configured is derived from it instead of env."""
    runtime = get_settings()
    if db_cookie is not None:
        vinted_configured = is_value_configured(db_cookie, placeholder="put-your-vinted-cookie-here")
    else:
        vinted_configured = is_value_configured(runtime.vinted_cookie, placeholder="put-your-vinted-cookie-here")
    effective_telegram_bot_token = telegram_bot_token if telegram_bot_token is not None else runtime.telegram_bot_token
    effective_telegram_chat_id = telegram_chat_id if telegram_chat_id is not None else runtime.telegram_chat_id
    telegram_configured = is_value_configured(
        effective_telegram_bot_token,
        placeholder="put-your-telegram-bot-token-here",
    ) and is_value_configured(effective_telegram_chat_id, placeholder="put-your-telegram-chat-id-here")
    ai_configured = is_value_configured(runtime.ai_api_key, placeholder="put-your-ai-key-here")
    return vinted_configured, telegram_configured, ai_configured


def ai_configuration(
    *,
    judge_model: AiModelConfigRow | None,
    judge_fallback_model: AiModelConfigRow | None,
    learn_model: AiModelConfigRow | None,
) -> tuple[bool, bool, bool]:
    """Returns (ai_configured, judge_configured, learning_configured) derived from whether the
    referenced registry rows exist. A `None` model id (no row resolved) means "not configured"."""
    runtime = get_settings()
    openai_ready = is_value_configured(runtime.ai_api_key, placeholder="put-your-ai-key-here")
    cerebras_ready = is_value_configured(runtime.cerebras_api_key, placeholder="put-your-cerebras-api-key-here")
    openrouter_ready = is_value_configured(
        runtime.openrouter_api_key, placeholder="put-your-openrouter-api-key-here"
    )

    def _model_ready(model: AiModelConfigRow | None) -> bool:
        if model is None:
            return False
        if model.provider == "openai":
            return openai_ready
        if model.provider == "cerebras":
            return cerebras_ready
        if model.provider == "openrouter":
            return openrouter_ready
        return bool(model.local_base_url and model.local_base_url.strip())

    judge_configured = _model_ready(judge_model)
    if judge_fallback_model is not None:
        judge_configured = judge_configured and _model_ready(judge_fallback_model)
    learning_configured = _model_ready(learn_model)
    return judge_configured and learning_configured, judge_configured, learning_configured


def build_session_health(*, region: str) -> SessionHealth:
    vinted_configured, _, _ = integration_configuration()
    return SessionHealth(
        region=region,
        status="warning" if vinted_configured else "missing",
        last_validated_at=None,
        detail=(
            "Vinted credentials are present. Live session validation still needs to confirm the cookie against the upstream service."
            if vinted_configured
            else "Vinted credentials are missing from the environment. Add a valid cookie to enable live scans."
        ),
    )


def as_aware(value: datetime | None) -> datetime | None:
    # SQLite returns DateTime(timezone=True) columns as naive datetimes, but values
    # set within the current session (e.g. datetime.now(UTC)) are tz-aware. Treat
    # stored naive timestamps as UTC so the two can be compared without TypeError.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def timestamp_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, UTC)


def _coerce_alert_threshold(value: int | None, *, fallback: int = 95) -> int:
    if value is None:
        value = fallback
    parsed = int(value)
    return max(1, min(100, parsed))


def search_to_record(model: Search, *, default_alert_threshold: int = 95) -> SearchRecord:
    effective_alert_threshold = _coerce_alert_threshold(
        model.alert_threshold,
        fallback=_coerce_alert_threshold(default_alert_threshold),
    )
    clothing_item = model.clothing_item
    name = model.name
    if clothing_item in CLOTHING_ITEM_LABELS:
        name = CLOTHING_ITEM_LABELS[cast(ClothingItem, clothing_item)]
    return SearchRecord.model_validate(
        {
            "id": model.id,
            "name": name,
            "enabled": model.enabled,
            "clothing_item": clothing_item,
            "query": model.query,
            "region": model.region,
            "filters": model.filters or [],
            "alert_threshold": model.alert_threshold,
            "effective_alert_threshold": effective_alert_threshold,
            "last_run_at": model.last_run_at,
            "last_found_count": model.last_found_count,
            "last_fetched_count": model.last_fetched_count,
            "last_judged_count": model.last_judged_count,
        }
    )


def candidate_to_contract(model: Candidate) -> CandidateRecord:
    latest_delivery = None
    if model.alert_deliveries:
        latest_delivery = sorted(model.alert_deliveries, key=lambda d: d.created_at, reverse=True)[0]

    return CandidateRecord.model_validate(
        {
            "id": model.id,
            "external_item_id": model.external_item_id,
            "clothing_item": model.clothing_item,
            "title": model.title,
            "brand": model.brand,
            "price_eur": model.price_eur,
            "size": model.size,
            "url": model.url,
            "description": (model.normalized_listing or {}).get("description", "") or "",
            "image_urls": model.image_urls or [],
            "source_search_id": model.search_id,
            "source_search_name": model.search.name if model.search else None,
            "source_region": model.source_region,
            "matched_filters": model.matched_filters or [],
            "matched_preferences": model.matched_preferences or [],
            "features": model.features or [],
            "normalized_listing": model.normalized_listing or {},
            "first_seen_at": model.first_seen_at,
            "last_seen_at": model.last_seen_at,
            "last_scan_mode": model.last_scan_mode,
            "extraction_status": model.extraction_status,
            "extraction_error": model.extraction_error,
            "telegram_delivery_status": latest_delivery.status if latest_delivery else "not_queued",
            "telegram_delivery_attempt_count": latest_delivery.attempt_count if latest_delivery else 0,
            "telegram_delivery_last_error": latest_delivery.last_error if latest_delivery else None,
            "telegram_delivery_queued_at": latest_delivery.created_at if latest_delivery else None,
            "telegram_delivery_sent_at": latest_delivery.sent_at if latest_delivery else None,
            "score_trace": model.score_trace or {},
            "ai_observation": model.ai_observation or {},
            "grading_stage": model.grading_stage or "vlm_judged",
            "feedback": model.feedback,
            "feedback_comment": model.feedback_comment or "",
            "created_at": model.created_at,
        }
    )


def learning_snapshot_to_contract(model: LearningSnapshotState) -> LearningSnapshot:
    payload: dict | list[dict] = model.changed_weights or {}
    metadata: dict = payload if isinstance(payload, dict) else {}
    changed_weights = [] if isinstance(payload, dict) else payload
    summary = model.summary or metadata.get("summary") or model.reason
    old_prompt = model.old_prompt if model.old_prompt is not None else metadata.get("old_prompt")
    new_prompt = model.new_prompt if model.new_prompt is not None else metadata.get("new_prompt")
    old_character_count, old_token_count = text_counts(old_prompt) if old_prompt is not None else (None, None)
    new_character_count, new_token_count = text_counts(new_prompt) if new_prompt is not None else (None, None)
    return LearningSnapshot.model_validate(
        {
            "id": model.id,
            "created_at": model.created_at,
            "reason": model.reason,
            "changed_weights": changed_weights,
            "summary": summary,
            "old_prompt": old_prompt,
            "new_prompt": new_prompt,
            "old_taste_profile": model.old_taste_profile,
            "new_taste_profile": model.new_taste_profile,
            "old_prompt_character_count": old_character_count,
            "old_prompt_token_count": old_token_count,
            "new_prompt_character_count": new_character_count,
            "new_prompt_token_count": new_token_count,
            "source_counts": model.source_counts or metadata.get("source_counts", {}),
        }
    )


def settings_to_contract(model: AppSettingsState, session: Session) -> SettingsSnapshot:
    runtime = get_settings()
    telegram_bot_token = model.telegram_bot_token or runtime.telegram_bot_token
    telegram_chat_id = model.telegram_chat_id or runtime.telegram_chat_id
    telegram_webhook_url = model.telegram_webhook_url or runtime.telegram_webhook_url
    telegram_webhook_secret = model.telegram_webhook_secret or runtime.telegram_webhook_secret
    vinted_configured, telegram_configured, _ = integration_configuration(
        db_cookie=model.vinted_cookie,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
    )

    model_rows = session.scalars(select(AiModelConfigRow)).all()
    models_by_id = {row.id: row for row in model_rows}
    judge_model = models_by_id.get(model.judge_model_id) if model.judge_model_id else None
    judge_fallback_model = models_by_id.get(model.judge_fallback_model_id) if model.judge_fallback_model_id else None
    learn_model = models_by_id.get(model.learn_model_id) if model.learn_model_id else None

    ai_configured, judge_configured, learning_configured = ai_configuration(
        judge_model=judge_model,
        judge_fallback_model=judge_fallback_model,
        learn_model=learn_model,
    )
    return SettingsSnapshot.model_validate(
        {
            "vinted_region": model.vinted_region,
            "vinted_cookie": model.vinted_cookie or "",
            "vinted_refresh_token": model.vinted_refresh_token or "",
            "telegram_bot_token": telegram_bot_token,
            "telegram_chat_id": telegram_chat_id,
            "telegram_webhook_url": telegram_webhook_url,
            "telegram_webhook_secret": telegram_webhook_secret,
            "vinted_configured": vinted_configured,
            "telegram_configured": telegram_configured,
            "ai_configured": ai_configured,
            "judge_configured": judge_configured,
            "learning_configured": learning_configured,
            "judge_model_id": model.judge_model_id,
            "judge_fallback_model_id": model.judge_fallback_model_id,
            "learn_model_id": model.learn_model_id,
            "observation_model_id": model.observation_model_id,
            "models": [
                AiModelConfig.model_validate(
                    {
                        "id": row.id,
                        "provider": row.provider,
                        "model_name": row.model_name,
                        "reasoning_effort": row.reasoning_effort,
                        "local_base_url": row.local_base_url,
                        "display_name": row.display_name,
                        "created_at": row.created_at,
                        "updated_at": row.updated_at,
                    }
                )
                for row in model_rows
            ],
            "vlm_grid_size": model.vlm_grid_size,
            "vlm_pack_multiple_listing_images": (
                True
                if getattr(model, "vlm_pack_multiple_listing_images", True) is None
                else model.vlm_pack_multiple_listing_images
            ),
            "vlm_judge_parallel_requests": model.vlm_judge_parallel_requests,
            "ai_judge_image_max_px": model.ai_judge_image_max_px,
            "alert_threshold": _coerce_alert_threshold(getattr(model, "alert_threshold", 95)),
            "scan_interval_seconds": getattr(model, "scan_interval_seconds", 1800),
            "blocked_brands": list(model.blocked_brands or []),
            "session_health": model.session_health or build_session_health(region=model.vinted_region).model_dump(mode="json"),
        }
    )


def taste_sample_to_contract(model: TasteSampleState) -> TasteSample:
    image_urls = list(model.image_urls or [])
    if model.storage_path and not image_urls:
        image_urls = [f"/api/taste/samples/{model.id}/image"]
    return TasteSample.model_validate(
        {
            "id": model.id,
            "kind": model.kind,
            "clothing_item": model.clothing_item,
            "note": model.note or "",
            "file_name": model.file_name or "",
            "storage_path": model.storage_path,
            "vinted_url": model.vinted_url,
            "external_item_id": model.external_item_id,
            "title": model.title or "",
            "brand": model.brand or "",
            "price_eur": model.price_eur,
            "size": model.size or "",
            "description": model.description or "",
            "image_urls": image_urls,
            "cached_image_paths": model.cached_image_paths or [],
            "stored_image_paths": model.stored_image_paths or [],
            "image_observations": model.image_observations or [],
            "normalized_listing": model.normalized_listing or {},
            "candidate_id": model.candidate_id,
            "created_at": model.created_at,
            "updated_at": model.updated_at,
        }
    )


def taste_state_to_snapshot(
    state: TasteState,
    samples: list[TasteSampleState],
    *,
    latest_snapshot: LearningSnapshot | None = None,
) -> TasteSnapshot:
    last_recomputed_at = as_aware(state.last_recomputed_at)
    manual_note_updated_at = as_aware(state.manual_note_updated_at)
    # Use recompute_started_at as the dirty-check anchor so votes cast while a recompute
    # is in flight are counted as pending rather than silently absorbed into the result.
    dirty_anchor = as_aware(state.recompute_started_at) or last_recomputed_at

    def _sample_changed(item: TasteSampleState) -> bool:
        if dirty_anchor is None:
            return True
        updated_at = as_aware(item.updated_at)
        return updated_at is not None and updated_at > dirty_anchor

    changed_samples = [item for item in samples if _sample_changed(item)]
    return TasteSnapshot(
        samples=[taste_sample_to_contract(item) for item in samples],
        manual_note=TasteManualNote(text=state.manual_note or "", updated_at=manual_note_updated_at),
        taste_profile=TasteProfile.model_validate(state.taste_profile) if state.taste_profile else None,
        reference_observations=[
            ReferenceObservation.model_validate(item) for item in (state.reference_observations or [])
        ],
        latest_learning_snapshot=latest_snapshot,
        last_recomputed_at=last_recomputed_at,
        dirty_counts=TasteDirtyCounts(
            new_or_changed_samples=len(changed_samples),
            new_or_changed_positive_samples=sum(1 for item in changed_samples if item.kind in {"wardrobe", "offer_like"}),
            new_or_changed_negative_samples=sum(1 for item in changed_samples if item.kind == "offer_dislike"),
            manual_note_changed=bool(
                manual_note_updated_at
                and (dirty_anchor is None or manual_note_updated_at > dirty_anchor)
            ),
        ),
        recompute_state=TasteRecomputeState.model_validate(
            {
                "status": state.recompute_status or "idle",
                "job_id": state.recompute_job_id,
                "started_at": state.recompute_started_at,
                "finished_at": state.recompute_finished_at,
                "error": state.recompute_error,
                "last_cost_usd": float(state.last_recompute_cost_usd or 0.0),
                "last_input_tokens": int(state.last_recompute_input_tokens or 0),
                "last_output_tokens": int(state.last_recompute_output_tokens or 0),
            }
        ),
    )
