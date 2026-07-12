from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator  # HttpUrl kept for validated inputs only


MaintenanceMode = Literal["idle", "manual", "import"]
ScanMode = Literal["preview", "live"]
ExtractionStatus = Literal["pending", "completed", "failed"]
DeliveryStatus = Literal["not_queued", "pending", "processing", "sent", "failed"]
AiProvider = Literal["openai", "cerebras", "local", "openrouter"]
GridPosition = Literal[
    "top_left",
    "top_center",
    "top_right",
    "middle_left",
    "middle_center",
    "middle_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
]
TasteSampleKind = Literal["wardrobe", "offer_like", "offer_dislike", "offer_note"]
ClothingItem = Literal["schuhe", "hosen", "obenrum_warm", "obenrum_mittel", "obenrum_kalt", "kopf"]

CLOTHING_ITEM_LABELS: dict[ClothingItem, str] = {
    "schuhe": "Schuhe",
    "hosen": "Hosen",
    "obenrum_warm": "Obenrum Warm",
    "obenrum_mittel": "Obenrum Mittel",
    "obenrum_kalt": "Obenrum Kalt",
    "kopf": "Kopf",
}

CLOTHING_ITEM_DESCRIPTIONS: dict[ClothingItem, str] = {
    "schuhe": "Shoes and sneakers.",
    "hosen": "Trousers, jeans, cargos, shorts, and other legwear.",
    "obenrum_warm": "Warm-weather tops such as T-shirts and short-sleeve shirts.",
    "obenrum_mittel": "Medium-layer tops such as longsleeves and light pullovers.",
    "obenrum_kalt": "Cold-weather upper-body pieces such as heavy pullovers and jackets.",
    "kopf": "Headwear, especially funny or weird baseball caps.",
}


class SearchFilter(BaseModel):
    field: str
    label: str
    values: list[str]
    mode: Literal["include", "exclude", "range", "exact"] = "include"


class SearchCategoryOption(BaseModel):
    label: str
    description: str
    default_aliases: list[str]
    allowed_aliases: list[str]
    alias_catalog_ids: dict[str, list[str]] = Field(default_factory=dict)
    resolved_catalog_ids: list[str]


class SearchRecord(BaseModel):
    id: str
    name: str
    enabled: bool = True
    clothing_item: ClothingItem
    query: str
    region: str
    filters: list[SearchFilter] = Field(default_factory=list)
    alert_threshold: int | None = None
    effective_alert_threshold: int = 95
    last_run_at: datetime | None = None
    last_found_count: int = 0
    last_fetched_count: int = 0
    last_judged_count: int = 0


class GeneratedSearchDraft(BaseModel):
    id: str
    clothing_item: ClothingItem
    name: str
    query: str
    region: str
    filters: list[SearchFilter] = Field(default_factory=list)
    rationale: str = ""
    created_at: datetime


class ClothingItemTasteProfile(BaseModel):
    clothing_item: ClothingItem
    label: str
    summary: str
    taste_prompt: str
    core_aesthetic_summary: str = ""
    cross_item_influence: list[str] = Field(default_factory=list)
    likes: list[str] = Field(default_factory=list)
    dislikes_or_penalties: list[str] = Field(default_factory=list)
    instant_alert_examples: list[str] = Field(default_factory=list)
    instant_reject_examples: list[str] = Field(default_factory=list)
    scoring_rubric: dict[str, str] = Field(default_factory=dict)
    transparency_labels: list[str] = Field(default_factory=list)
    generated_search: GeneratedSearchDraft | None = None
    source_counts: dict[str, int] = Field(default_factory=dict)


class TasteProfile(BaseModel):
    version: int = 1
    summary: str
    taste_prompt: str
    core_aesthetic_summary: str = ""
    item_profiles: dict[ClothingItem, ClothingItemTasteProfile] = Field(default_factory=dict)
    likes: list[str] = Field(default_factory=list)
    dislikes_or_penalties: list[str] = Field(default_factory=list)
    instant_alert_examples: list[str] = Field(default_factory=list)
    instant_reject_examples: list[str] = Field(default_factory=list)
    scoring_rubric: dict[str, str] = Field(default_factory=dict)
    transparency_labels: list[str] = Field(default_factory=list)
    generated_searches: list[GeneratedSearchDraft] = Field(default_factory=list)
    source_counts: dict[str, int] = Field(default_factory=dict)
    model: str | None = None
    reasoning_effort: str | None = None
    generated_at: datetime | None = None


class TasteSample(BaseModel):
    id: str
    kind: TasteSampleKind
    clothing_item: ClothingItem
    note: str = ""
    file_name: str = ""
    storage_path: str | None = None
    vinted_url: str | None = None
    external_item_id: str | None = None
    title: str = ""
    brand: str = ""
    price_eur: float | None = None
    size: str = ""
    description: str = ""
    image_urls: list[str] = Field(default_factory=list)
    cached_image_paths: list[str] = Field(default_factory=list)
    stored_image_paths: list[str] = Field(default_factory=list)
    image_observations: list[dict] = Field(default_factory=list)
    normalized_listing: dict = Field(default_factory=dict)
    candidate_id: str | None = None
    created_at: datetime
    updated_at: datetime


class TasteManualNote(BaseModel):
    text: str = ""
    updated_at: datetime | None = None


class TasteDirtyCounts(BaseModel):
    new_or_changed_samples: int = 0
    new_or_changed_positive_samples: int = 0
    new_or_changed_negative_samples: int = 0
    manual_note_changed: bool = False


class TasteRecomputeState(BaseModel):
    status: Literal["idle", "running", "succeeded", "failed", "cancelled"] = "idle"
    job_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    last_cost_usd: float = 0.0
    last_input_tokens: int = 0
    last_output_tokens: int = 0


class TasteSnapshot(BaseModel):
    samples: list[TasteSample] = Field(default_factory=list)
    manual_note: TasteManualNote = Field(default_factory=TasteManualNote)
    taste_profile: TasteProfile | None = None
    reference_observations: list[ReferenceObservation] = Field(default_factory=list)
    latest_learning_snapshot: LearningSnapshot | None = None
    last_recomputed_at: datetime | None = None
    dirty_counts: TasteDirtyCounts = Field(default_factory=TasteDirtyCounts)
    recompute_state: TasteRecomputeState = Field(default_factory=TasteRecomputeState)


class TasteSampleUpdate(BaseModel):
    note: str | None = None
    kind: TasteSampleKind | None = None
    clothing_item: ClothingItem | None = None


class TasteManualNoteUpdate(BaseModel):
    text: str = ""


class TasteOfferCreate(BaseModel):
    vinted_url: str | None = None
    kind: Literal["offer_like", "offer_dislike"] = "offer_like"
    clothing_item: ClothingItem
    note: str = ""
    title: str = ""
    description: str = ""
    image_urls: list[str] = Field(default_factory=list)


class WardrobeZipImportEntry(BaseModel):
    file: str
    clothing_item: ClothingItem
    note: str = ""


class WardrobeZipManifest(BaseModel):
    images: list[WardrobeZipImportEntry]


class WardrobeZipImportResult(BaseModel):
    imported: list[TasteSample]
    skipped: list[str]


class ReferenceObservation(BaseModel):
    image_id: str
    file_name: str
    clothing_item: ClothingItem
    garment_type: str = ""
    silhouette_and_cut: str = ""
    color_palette: str = ""
    fabric_and_texture: str = ""
    prints_or_patterns: str = ""
    details_and_hardware: str = ""
    era_or_subculture: str = ""
    vibe_keywords: list[str] = Field(default_factory=list)


class TasteObservationCacheStats(BaseModel):
    total_image_inputs: int = 0
    cached_observations: int = 0
    fresh_observations: int = 0
    observation_provider: AiProvider = "openai"
    observation_model: str = ""
    profile_model: str = ""


class TasteRecomputeResult(BaseModel):
    snapshot: TasteSnapshot
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    observation_cache: TasteObservationCacheStats = Field(default_factory=TasteObservationCacheStats)


class SearchDraftApplyResult(BaseModel):
    profile_version: int | None = None
    requested_profile_version: int | None = None
    stale: bool = False
    applied_searches: int = 0
    unchanged_searches: int = 0
    skipped_searches: int = 0
    applied: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    summary: str


class CandidateJudgment(BaseModel):
    position: GridPosition
    score: int = Field(ge=1, le=100)
    explanation: str
    labels: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)


class FeatureWeight(BaseModel):
    key: str
    label: str
    weight: float
    source: Literal["seed", "image_analysis", "note_analysis", "feedback_learning"]


class ScoreTrace(BaseModel):
    final_score: float
    score_10: int = 0
    threshold: float
    decision: Literal["alert", "review", "discard"]
    summary: str
    explanation: str = ""
    labels: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    model: str | None = None
    prompt_version: int | None = None
    grid_batch_id: str | None = None
    grid_position: GridPosition | None = None
    judged_at: datetime | None = None
    raw_response: dict = Field(default_factory=dict)


class CandidateFeature(BaseModel):
    key: str
    label: str
    value: str
    signal_strength: float
    source: Literal["listing", "image_model", "text_model", "feedback"]


class CandidateRecord(BaseModel):
    id: str
    external_item_id: str | None = None
    clothing_item: ClothingItem
    title: str
    brand: str
    price_eur: float
    size: str
    url: str
    description: str = ""
    image_urls: list[str] = Field(default_factory=list)
    source_search_id: str
    source_search_name: str | None = None
    source_region: str | None = None
    matched_filters: list[str] = Field(default_factory=list)
    matched_preferences: list[str] = Field(default_factory=list)
    features: list[CandidateFeature] = Field(default_factory=list)
    normalized_listing: dict = Field(default_factory=dict)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_scan_mode: ScanMode | None = None
    extraction_status: ExtractionStatus = "pending"
    extraction_error: str | None = None
    telegram_delivery_status: DeliveryStatus = "not_queued"
    telegram_delivery_attempt_count: int = 0
    telegram_delivery_last_error: str | None = None
    telegram_delivery_queued_at: datetime | None = None
    telegram_delivery_sent_at: datetime | None = None
    score_trace: ScoreTrace
    ai_observation: dict = Field(default_factory=dict)
    grading_stage: Literal["vlm_judged", "failed"] = "vlm_judged"
    feedback: Literal["like", "dislike", "unknown"] = "unknown"
    feedback_comment: str = ""
    created_at: datetime


class CandidatePage(BaseModel):
    items: list[CandidateRecord] = Field(default_factory=list)
    total: int = 0
    stage_counts: dict[str, int] = Field(default_factory=dict)
    item_counts: dict[str, int] = Field(default_factory=dict)


class LearningSnapshot(BaseModel):
    id: str
    created_at: datetime
    reason: str
    changed_weights: list[FeatureWeight] = Field(default_factory=list)
    summary: str = ""
    old_prompt: str | None = None
    new_prompt: str | None = None
    old_taste_profile: TasteProfile | None = None
    new_taste_profile: TasteProfile | None = None
    old_prompt_character_count: int | None = None
    old_prompt_token_count: int | None = None
    new_prompt_character_count: int | None = None
    new_prompt_token_count: int | None = None
    prompt_tokenizer: Literal["o200k_base"] = "o200k_base"
    source_counts: dict[str, int] = Field(default_factory=dict)


class JudgmentPromptPreview(BaseModel):
    prompt: str
    clothing_item: ClothingItem
    profile_version: int
    character_count: int
    token_count: int
    tokenizer: Literal["o200k_base"] = "o200k_base"


class SessionHealth(BaseModel):
    region: str
    status: Literal["healthy", "warning", "missing"]
    last_validated_at: datetime | None
    detail: str


class AiModelConfig(BaseModel):
    id: str
    provider: AiProvider
    model_name: str
    reasoning_effort: str
    local_base_url: str | None = None
    display_name: str
    created_at: datetime
    updated_at: datetime


class AiModelCreate(BaseModel):
    provider: AiProvider
    model_name: str = Field(min_length=1, max_length=128)
    reasoning_effort: str = Field(min_length=1, max_length=32)
    local_base_url: str | None = None

    @model_validator(mode="after")
    def _validate_local_base_url(self) -> "AiModelCreate":
        if self.provider == "local" and not (self.local_base_url and self.local_base_url.strip()):
            raise ValueError("local_base_url is required when provider is 'local'.")
        return self


class AiModelUpdate(BaseModel):
    # provider is immutable after creation (the registry UI never lets it be changed on
    # edit), so this is a partial update over the remaining fields only.
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)
    local_base_url: str | None = None


class SettingsSnapshot(BaseModel):
    vinted_region: str
    vinted_cookie: str = ""
    vinted_refresh_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str = ""
    vinted_configured: bool
    telegram_configured: bool
    ai_configured: bool
    judge_configured: bool
    learning_configured: bool
    judge_model_id: str | None = None
    judge_fallback_model_id: str | None = None
    learn_model_id: str | None = None
    observation_model_id: str | None = None
    models: list[AiModelConfig] = Field(default_factory=list)
    vlm_grid_size: int
    vlm_pack_multiple_listing_images: bool = True
    vlm_judge_parallel_requests: int = 1
    ai_judge_image_max_px: int = 512
    alert_threshold: int = 95
    scan_interval_seconds: int = 1800
    blocked_brands: list[str] = Field(default_factory=list)
    session_health: SessionHealth


class DashboardStats(BaseModel):
    active_searches: int
    candidates_today: int
    likes: int
    dislikes: int
    avg_alert_score: float
    pending_deliveries: int = 0
    failed_deliveries: int = 0
    last_successful_scan_at: datetime | None = None


class ScoreDistributionBin(BaseModel):
    min_score: int
    max_score: int
    count: int
    percentage: float


class ScoreDistribution(BaseModel):
    window: Literal["1h", "6h", "12h", "1d", "7d", "30d", "all"]
    total_count: int
    bins: list[ScoreDistributionBin]


class TransparencySnapshot(BaseModel):
    system_prompt: str
    user_prompt: str
    taste_profile: TasteProfile | None = None
    reference_observations: list[ReferenceObservation] = Field(default_factory=list)
    extracted_attributes: list[str]
    weights: list[FeatureWeight]
    latest_learning_snapshot: LearningSnapshot | None = None


class SearchUpdate(BaseModel):
    name: str = ""
    clothing_item: ClothingItem
    query: str
    region: str
    filters: list[SearchFilter] = Field(default_factory=list)
    alert_threshold: int | None = None
    enabled: bool = True

    @field_validator("region")
    @classmethod
    def _validate_de_region(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "de":
            raise ValueError("Only the de Vinted region is supported.")
        return normalized

    @field_validator("alert_threshold")
    @classmethod
    def _validate_alert_threshold(cls, value: int | None) -> int | None:
        if value is not None and not (1 <= value <= 100):
            raise ValueError("Alert threshold must be between 1 and 100.")
        return value


class SearchRunResult(BaseModel):
    search_id: str
    mode: ScanMode
    fetched_candidates: int
    alert_candidates: int
    queued_alert_deliveries: int = 0
    run_id: int | None = None
    summary: str


class DeliveryProcessingResult(BaseModel):
    eligible_deliveries: int = 0
    processed_deliveries: int = 0
    sent_deliveries: int = 0
    retry_scheduled_deliveries: int = 0
    failed_deliveries: int = 0
    skipped_reason: str | None = None
    summary: str


class FeedbackPayload(BaseModel):
    verdict: Literal["like", "dislike"] | None = None
    comment: str = ""
    skip_if_unchanged: bool = False


class AiCategoryStats(BaseModel):
    total_usd: float
    last_24h_usd: float
    last_7d_usd: float
    last_30d_usd: float
    total_calls: int
    last_24h_calls: int
    last_7d_calls: int
    last_30d_calls: int


class AiCostStats(BaseModel):
    total_usd: float
    last_24h_usd: float
    last_7d_usd: float
    last_30d_usd: float
    total_calls: int
    last_24h_calls: int
    last_7d_calls: int
    last_30d_calls: int
    judge: AiCategoryStats
    learning: AiCategoryStats


class StorageCategoryStats(BaseModel):
    bytes: int
    file_count: int


class StorageStats(BaseModel):
    total_bytes: int
    database: StorageCategoryStats
    uploads: StorageCategoryStats
    feedback_assets: StorageCategoryStats
    cache_candidate_images: StorageCategoryStats
    cache_taste_offers: StorageCategoryStats
    cache_other: StorageCategoryStats


class CacheClearResult(BaseModel):
    bytes_freed: int
    files_removed: int


class TelegramFeedbackCallback(BaseModel):
    delivery_id: str
    verdict: Literal["like", "dislike"]


class TelegramTasteCallback(BaseModel):
    action: Literal["recompute", "apply_drafts", "skip_drafts"]
    profile_version: int | None = None


class TelegramChat(BaseModel):
    id: int | str


class TelegramCallbackMessage(BaseModel):
    message_id: int
    text: str | None = None
    chat: TelegramChat | None = None


class TelegramCallbackQuery(BaseModel):
    id: str
    data: str | None = None
    message: TelegramCallbackMessage | None = None


class TelegramReplyToMessage(BaseModel):
    message_id: int


class TelegramMessage(BaseModel):
    message_id: int
    text: str | None = None
    chat: TelegramChat | None = None
    reply_to_message: TelegramReplyToMessage | None = None


class TelegramUpdate(BaseModel):
    update_id: int | None = None
    callback_query: TelegramCallbackQuery | None = None
    message: TelegramMessage | None = None


class TelegramWebhookResult(BaseModel):
    ok: bool = True
    action: Literal[
        "ignored",
        "feedback_recorded",
        "feedback_unchanged",
        "feedback_queued",
        "invalid_callback",
        "unauthorized",
        "taste_status_sent",
        "taste_recompute_started",
        "taste_recompute_already_running",
        "taste_drafts_applied",
        "taste_drafts_skipped",
    ]
    detail: str
    candidate_id: str | None = None
    delivery_id: str | None = None
    verdict: Literal["like", "dislike"] | None = None
    learning_snapshot_id: str | None = None
    recompute_job_id: str | None = None
    profile_version: int | None = None
    changed_searches: int | None = None


class TelegramWebhookRegistrationPayload(BaseModel):
    url: HttpUrl | None = None
    drop_pending_updates: bool = False


class TelegramWebhookStatus(BaseModel):
    last_check_ok: bool
    is_registered: bool
    matches_configured_url: bool
    configured_url: str | None = None
    effective_url: str | None = None
    has_secret_token: bool = False
    pending_update_count: int = 0
    allowed_updates: list[str] = Field(default_factory=list)
    last_error_message: str | None = None
    last_error_at: datetime | None = None
    checked_at: datetime
    detail: str


class SettingsUpdate(BaseModel):
    vinted_region: str
    vinted_cookie: str | None = None
    vinted_refresh_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: str | None = None
    judge_model_id: str | None = None
    judge_fallback_model_id: str | None = None
    learn_model_id: str | None = None
    observation_model_id: str | None = None
    vlm_grid_size: int | None = None
    vlm_pack_multiple_listing_images: bool | None = None
    vlm_judge_parallel_requests: int | None = None
    ai_judge_image_max_px: int | None = None
    alert_threshold: int | None = None
    scan_interval_seconds: int | None = None

    @field_validator("vinted_region")
    @classmethod
    def _validate_de_region(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "de":
            raise ValueError("Only the de Vinted region is supported.")
        return normalized

    @field_validator("vlm_grid_size")
    @classmethod
    def _validate_vlm_grid_size(cls, value: int | None) -> int | None:
        if value is not None and value not in {1, 4, 9}:
            raise ValueError("VLM grid size must be 1, 4, or 9.")
        return value

    @field_validator("vlm_judge_parallel_requests")
    @classmethod
    def _validate_vlm_judge_parallel_requests(cls, value: int | None) -> int | None:
        if value is not None and not (1 <= value <= 16):
            raise ValueError("VLM parallel requests must be between 1 and 16.")
        return value

    @field_validator("ai_judge_image_max_px")
    @classmethod
    def _validate_image_max_px(cls, value: int | None) -> int | None:
        if value is not None and not (64 <= value <= 2048):
            raise ValueError("Image max px must be between 64 and 2048.")
        return value

    @field_validator("alert_threshold")
    @classmethod
    def _validate_alert_threshold(cls, value: int | None) -> int | None:
        if value is not None and not (1 <= value <= 100):
            raise ValueError("Alert threshold must be between 1 and 100.")
        return value

    @field_validator("scan_interval_seconds")
    @classmethod
    def _validate_scan_interval_seconds(cls, value: int | None) -> int | None:
        if value is not None and not (30 <= value <= 86400):
            raise ValueError("Scan interval must be between 30 and 86400 seconds.")
        return value


class BlockedBrandsSnapshot(BaseModel):
    brands: list[str] = Field(default_factory=list)


class BlockedBrandsUpdate(BaseModel):
    brands: list[str] = Field(default_factory=list)


class VintedBrandOption(BaseModel):
    id: str
    title: str


class ModelTestRequest(BaseModel):
    model_id: str
    prompt: str = Field(min_length=1, max_length=4000)


class ModelTestResult(BaseModel):
    ok: bool = True
    provider: str
    base_url: str | None = None
    model: str
    answer: str


class VintedSizesResult(BaseModel):
    sizes: list[str]
    region: str


class LabeledExample(BaseModel):
    """A past candidate the user verdicted, with its stored image observation."""

    candidate_id: str
    clothing_item: ClothingItem
    verdict: Literal["like", "dislike"]
    title: str
    brand: str
    description: str = ""
    user_comment: str = ""
    observation: ReferenceObservation | None = None


class BackupManifestFileEntry(BaseModel):
    """One file entry inside a full-state backup ZIP. `path` is relative to the
    backup archive root (e.g. `uploads/taste/abc.jpg`, `cache/candidate-images/x.jpg`).
    `sha256` lets import verify integrity before overwriting anything."""

    path: str
    sha256: str
    size: int


class BackupManifest(BaseModel):
    """Root manifest written to `manifest.json` at the top of every full-state backup ZIP."""

    format_version: int
    created_at: datetime
    alembic_head: str
    include_cache: bool
    db_sha256: str
    db_size: int
    files: list[BackupManifestFileEntry]


class BackupImportResult(BaseModel):
    """Result returned by the import endpoint — surfaces what was restored and any
    files skipped due to hash mismatch."""

    restored_db: bool
    restored_files: int
    skipped_files: list[str]
    alembic_head: str
    reloaded: bool


class WorkerHeartbeatRecord(BaseModel):
    owner: str = ""
    cycle_count: int = 0
    phase: str = ""
    last_heartbeat_at: datetime | None = None
    last_cycle_started_at: datetime | None = None
    last_cycle_finished_at: datetime | None = None


class WorkerActivityRecord(BaseModel):
    id: str
    operation: str
    started_at: datetime
    heartbeat_at: datetime


class SearchClaimStatus(BaseModel):
    search_id: str
    search_name: str
    clothing_item: ClothingItem
    run_status: str
    last_claimed_at: datetime | None = None
    last_run_at: datetime | None = None
    claim_age_seconds: float | None = None
    is_stale: bool = False


class DeliveryQueueSummary(BaseModel):
    pending: int = 0
    processing: int = 0
    sent: int = 0
    failed: int = 0
    latest_failures: list[CandidateRecord] = Field(default_factory=list)


class SearchRunRecord(BaseModel):
    id: int
    search_id: str
    mode: ScanMode
    trigger: str = "worker"
    status: str = "running"
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    fetched_count: int = 0
    judged_count: int = 0
    new_judged_count: int = 0
    alert_count: int = 0
    queued_delivery_count: int = 0
    failures_by_reason: dict[str, int] | None = None
    judge_provider: str | None = None
    judge_model: str | None = None
    fallback_used: bool = False
    vinted_status: str | None = None
    vinted_detail: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


class SearchRunPage(BaseModel):
    items: list[SearchRunRecord] = Field(default_factory=list)
    total: int = 0


class MaintenancePauseRequest(BaseModel):
    reason: str = ""


class OperationsSnapshot(BaseModel):
    maintenance_mode: MaintenanceMode = "idle"
    maintenance_reason: str = ""
    maintenance_started_at: datetime | None = None
    worker_heartbeat: WorkerHeartbeatRecord | None = None
    active_worker_tasks: list[WorkerActivityRecord] = Field(default_factory=list)
    recompute_status: str = "idle"
    delivery_summary: DeliveryQueueSummary = Field(default_factory=DeliveryQueueSummary)
    search_claims: list[SearchClaimStatus] = Field(default_factory=list)
    recent_runs: list[SearchRunRecord] = Field(default_factory=list)
