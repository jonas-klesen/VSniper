export type SearchFilter = {
  field: string;
  label: string;
  values: string[];
  mode: 'include' | 'exclude' | 'range' | 'exact';
};

export type ClothingItem = 'schuhe' | 'hosen' | 'obenrum_warm' | 'obenrum_mittel' | 'obenrum_kalt' | 'kopf';

export type SearchCategoryOption = {
  label: string;
  description: string;
  default_aliases: string[];
  allowed_aliases: string[];
  alias_catalog_ids?: Record<string, string[]>;
  resolved_catalog_ids: string[];
};

export type SearchCategoryOptions = Record<ClothingItem, SearchCategoryOption>;

export type SearchRecord = {
  id: string;
  name: string;
  enabled: boolean;
  clothing_item: ClothingItem;
  query: string;
  region: string;
  filters: SearchFilter[];
  alert_threshold: number | null;
  effective_alert_threshold: number;
  last_run_at: string | null;
  last_found_count: number;
  last_fetched_count: number;
  last_judged_count: number;
};

export type SearchUpdatePayload = {
  name?: string;
  clothing_item: ClothingItem;
  query: string;
  region: string;
  filters: SearchFilter[];
  alert_threshold: number | null;
  enabled: boolean;
};

export type FeatureWeight = {
  key: string;
  label: string;
  weight: number;
  source: 'seed' | 'image_analysis' | 'note_analysis' | 'feedback_learning';
};

export type TasteProfile = {
  version: number;
  summary: string;
  taste_prompt: string;
  core_aesthetic_summary: string;
  item_profiles: Partial<Record<ClothingItem, ClothingItemTasteProfile>>;
  likes: string[];
  dislikes_or_penalties: string[];
  instant_alert_examples: string[];
  instant_reject_examples: string[];
  scoring_rubric: Record<string, string>;
  transparency_labels: string[];
  generated_searches: GeneratedSearchDraft[];
  source_counts: Record<string, number>;
  model?: string | null;
  reasoning_effort?: string | null;
  generated_at?: string | null;
};

export type ClothingItemTasteProfile = {
  clothing_item: ClothingItem;
  label: string;
  summary: string;
  taste_prompt: string;
  core_aesthetic_summary: string;
  cross_item_influence: string[];
  likes: string[];
  dislikes_or_penalties: string[];
  instant_alert_examples: string[];
  instant_reject_examples: string[];
  scoring_rubric: Record<string, string>;
  transparency_labels: string[];
  generated_search?: GeneratedSearchDraft | null;
  source_counts: Record<string, number>;
};

export type GeneratedSearchDraft = {
  id: string;
  clothing_item: ClothingItem;
  name: string;
  query: string;
  region: string;
  filters: SearchFilter[];
  rationale: string;
  created_at: string;
};

export type TasteSample = {
  id: string;
  kind: 'wardrobe' | 'offer_like' | 'offer_dislike' | 'offer_note';
  clothing_item: ClothingItem;
  note: string;
  file_name: string;
  storage_path?: string | null;
  vinted_url?: string | null;
  external_item_id?: string | null;
  title: string;
  brand: string;
  price_eur?: number | null;
  size: string;
  description: string;
  image_urls: string[];
  cached_image_paths: string[];
  stored_image_paths: string[];
  image_observations: SampleImageObservation[];
  normalized_listing: Record<string, unknown>;
  candidate_id?: string | null;
  created_at: string;
  updated_at: string;
};

export type TasteSnapshot = {
  samples: TasteSample[];
  manual_note: { text: string; updated_at?: string | null };
  taste_profile?: TasteProfile | null;
  reference_observations: ReferenceObservation[];
  latest_learning_snapshot: LearningSnapshot | null;
  last_recomputed_at?: string | null;
  dirty_counts: {
    new_or_changed_samples: number;
    new_or_changed_positive_samples: number;
    new_or_changed_negative_samples: number;
    manual_note_changed: boolean;
  };
  recompute_state: {
    status: 'idle' | 'running' | 'succeeded' | 'failed' | 'cancelled';
    job_id?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
    error?: string | null;
    last_cost_usd: number;
    last_input_tokens: number;
    last_output_tokens: number;
  };
};

export type TasteRecomputeResult = {
  snapshot: TasteSnapshot;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  observation_cache: {
    total_image_inputs: number;
    cached_observations: number;
    fresh_observations: number;
    observation_provider: 'local' | 'openai';
    observation_model: string;
    profile_model: string;
  };
};

export type ReferenceObservation = {
  image_id: string;
  file_name: string;
  clothing_item: ClothingItem;
  garment_type: string;
  silhouette_and_cut: string;
  color_palette: string;
  fabric_and_texture: string;
  prints_or_patterns: string;
  details_and_hardware: string;
  era_or_subculture: string;
  vibe_keywords: string[];
};

// One entry per analysed image on a wardrobe/offer sample: the VLM's reading of that photo
// plus the provider/model metadata recorded when it was described.
export type SampleImageObservation = {
  image_id: string;
  file_name: string;
  clothing_item?: ClothingItem;
  provider?: string;
  model?: string;
  observed_at?: string;
  observation: ReferenceObservation;
};

export type ScoreTrace = {
  final_score: number;
  score_10: number;
  threshold: number;
  decision: 'alert' | 'review' | 'discard';
  summary: string;
  explanation: string;
  labels: string[];
  concerns: string[];
  model?: string | null;
  prompt_version?: number | null;
  grid_batch_id?: string | null;
  grid_position?: 'top_left' | 'top_center' | 'top_right' | 'middle_left' | 'middle_center' | 'middle_right' | 'bottom_left' | 'bottom_center' | 'bottom_right' | null;
  judged_at?: string | null;
  raw_response: Record<string, unknown>;
};

export type CandidateFeature = {
  key: string;
  label: string;
  value: string;
  signal_strength: number;
  source: 'listing' | 'image_model' | 'text_model' | 'feedback';
};

export type CandidateRecord = {
  id: string;
  external_item_id?: string | null;
  clothing_item: ClothingItem;
  title: string;
  brand: string;
  price_eur: number;
  size: string;
  url: string;
  description: string;
  image_urls: string[];
  source_search_id: string;
  source_search_name?: string | null;
  source_region?: string | null;
  matched_filters: string[];
  matched_preferences: string[];
  features: CandidateFeature[];
  normalized_listing: Record<string, unknown>;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  last_scan_mode?: 'preview' | 'live' | null;
  extraction_status: 'pending' | 'completed' | 'failed';
  extraction_error?: string | null;
  telegram_delivery_status: 'not_queued' | 'pending' | 'processing' | 'sent' | 'failed';
  telegram_delivery_attempt_count: number;
  telegram_delivery_last_error?: string | null;
  telegram_delivery_queued_at?: string | null;
  telegram_delivery_sent_at?: string | null;
  score_trace: ScoreTrace;
  ai_observation: Record<string, unknown>;
  grading_stage: 'vlm_judged' | 'failed';
  feedback: 'like' | 'dislike' | 'unknown';
  feedback_comment: string;
  created_at: string;
};

export type CandidatePage = {
  items: CandidateRecord[];
  total: number;
  stage_counts: Record<string, number>;
  item_counts: Record<string, number>;
};

export type DashboardStats = {
  active_searches: number;
  candidates_today: number;
  likes: number;
  dislikes: number;
  avg_alert_score: number;
  pending_deliveries: number;
  failed_deliveries: number;
  last_successful_scan_at?: string | null;
};

export type ScoreDistributionWindow = '1h' | '6h' | '12h' | '1d' | '7d' | '30d' | 'all';

export type ScoreDistributionBin = {
  min_score: number;
  max_score: number;
  count: number;
  percentage: number;
};

export type ScoreDistribution = {
  window: ScoreDistributionWindow;
  total_count: number;
  bins: ScoreDistributionBin[];
};

export type LearningSnapshot = {
  id: string;
  created_at: string;
  reason: string;
  changed_weights: FeatureWeight[];
  summary: string;
  old_prompt?: string | null;
  new_prompt?: string | null;
  old_taste_profile?: TasteProfile | null;
  new_taste_profile?: TasteProfile | null;
  old_prompt_character_count?: number | null;
  old_prompt_token_count?: number | null;
  new_prompt_character_count?: number | null;
  new_prompt_token_count?: number | null;
  prompt_tokenizer: 'o200k_base';
  source_counts: Record<string, number>;
};

export type JudgmentPromptPreview = {
  prompt: string;
  clothing_item: ClothingItem;
  profile_version: number;
  character_count: number;
  token_count: number;
  tokenizer: 'o200k_base';
};

export type TransparencySnapshot = {
  system_prompt: string;
  user_prompt: string;
  taste_profile?: TasteProfile | null;
  reference_observations: ReferenceObservation[];
  extracted_attributes: string[];
  weights: FeatureWeight[];
  latest_learning_snapshot: LearningSnapshot | null;
};

export type AiModelProvider = 'openai' | 'cerebras' | 'local' | 'openrouter';

export type ReasoningEffort = 'low' | 'medium' | 'high';

export type AiModelConfig = {
  id: string;
  provider: AiModelProvider;
  model_name: string;
  reasoning_effort: ReasoningEffort;
  local_base_url: string | null;
  display_name: string;
  created_at: string;
  updated_at: string;
};

export type AiModelCreate = {
  provider: AiModelProvider;
  model_name: string;
  reasoning_effort: ReasoningEffort;
  local_base_url?: string | null;
};

export type AiModelUpdate = {
  model_name?: string;
  reasoning_effort?: ReasoningEffort;
  local_base_url?: string | null;
};

export type SettingsSnapshot = {
  vinted_region: string;
  vinted_cookie: string;
  vinted_refresh_token: string;
  telegram_bot_token: string;
  telegram_chat_id: string;
  telegram_webhook_url: string;
  telegram_webhook_secret: string;
  vinted_configured: boolean;
  telegram_configured: boolean;
  ai_configured: boolean;
  judge_configured: boolean;
  learning_configured: boolean;
  judge_model_id: string | null;
  judge_fallback_model_id: string | null;
  learn_model_id: string | null;
  observation_model_id: string | null;
  models: AiModelConfig[];
  vlm_grid_size: number;
  vlm_pack_multiple_listing_images: boolean;
  vlm_judge_parallel_requests: number;
  ai_judge_image_max_px: number;
  alert_threshold: number;
  scan_interval_seconds: number;
  blocked_brands: string[];
  session_health: {
    region: string;
    status: 'healthy' | 'warning' | 'missing';
    last_validated_at: string | null;
    detail: string;
  };
};

export type TelegramWebhookStatus = {
  last_check_ok: boolean;
  is_registered: boolean;
  matches_configured_url: boolean;
  configured_url?: string | null;
  effective_url?: string | null;
  has_secret_token: boolean;
  pending_update_count: number;
  allowed_updates: string[];
  last_error_message?: string | null;
  last_error_at?: string | null;
  checked_at: string;
  detail: string;
};

export type SearchRunResult = {
  search_id: string;
  mode: 'preview' | 'live';
  fetched_candidates: number;
  alert_candidates: number;
  queued_alert_deliveries: number;
  summary: string;
};

export type AiCategoryStats = {
  total_usd: number;
  last_24h_usd: number;
  last_7d_usd: number;
  last_30d_usd: number;
  total_calls: number;
  last_24h_calls: number;
  last_7d_calls: number;
  last_30d_calls: number;
};

export type VintedSizesResult = {
  sizes: string[];
  region: string;
};

export type VintedBrandOption = {
  id: string;
  title: string;
};

export type WardrobeZipImportResult = {
  imported: TasteSample[];
  skipped: string[];
};

export type BackupImportResult = {
  restored_db: boolean;
  restored_files: number;
  skipped_files: string[];
  alembic_head: string;
  reloaded: boolean;
};

export type AiCostStats = {
  total_usd: number;
  last_24h_usd: number;
  last_7d_usd: number;
  last_30d_usd: number;
  total_calls: number;
  last_24h_calls: number;
  last_7d_calls: number;
  last_30d_calls: number;
  judge: AiCategoryStats;
  learning: AiCategoryStats;
};

export type StorageCategoryStats = {
  bytes: number;
  file_count: number;
};

export type StorageStats = {
  total_bytes: number;
  database: StorageCategoryStats;
  uploads: StorageCategoryStats;
  feedback_assets: StorageCategoryStats;
  cache_candidate_images: StorageCategoryStats;
  cache_taste_offers: StorageCategoryStats;
  cache_other: StorageCategoryStats;
};

export type CacheClearResult = {
  bytes_freed: number;
  files_removed: number;
};

export type WorkerHeartbeatRecord = {
  owner: string;
  cycle_count: number;
  phase: string;
  last_heartbeat_at: string | null;
  last_cycle_started_at: string | null;
  last_cycle_finished_at: string | null;
};

export type WorkerActivityRecord = {
  id: string;
  operation: string;
  started_at: string;
  heartbeat_at: string;
};

export type SearchClaimStatus = {
  search_id: string;
  search_name: string;
  clothing_item: ClothingItem;
  run_status: string;
  last_claimed_at: string | null;
  last_run_at: string | null;
  claim_age_seconds: number | null;
  is_stale: boolean;
};

export type DeliveryQueueSummary = {
  pending: number;
  processing: number;
  sent: number;
  failed: number;
  latest_failures: CandidateRecord[];
};

export type SearchRunRecord = {
  id: number;
  search_id: string;
  mode: 'preview' | 'live';
  trigger: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  fetched_count: number;
  judged_count: number;
  new_judged_count: number;
  alert_count: number;
  queued_delivery_count: number;
  failures_by_reason: Record<string, number> | null;
  judge_provider: string | null;
  judge_model: string | null;
  fallback_used: boolean;
  vinted_status: string | null;
  vinted_detail: string | null;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  error: string | null;
};

export type SearchRunPage = {
  items: SearchRunRecord[];
  total: number;
};

export type OperationsSnapshot = {
  maintenance_mode: 'idle' | 'manual' | 'import';
  maintenance_reason: string;
  maintenance_started_at: string | null;
  worker_heartbeat: WorkerHeartbeatRecord | null;
  active_worker_tasks: WorkerActivityRecord[];
  recompute_status: string;
  delivery_summary: DeliveryQueueSummary;
  search_claims: SearchClaimStatus[];
  recent_runs: SearchRunRecord[];
};
