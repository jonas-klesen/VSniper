import type {
  AiCostStats,
  AiModelConfig,
  AiModelCreate,
  AiModelUpdate,
  BackupImportResult,
  CacheClearResult,
  CandidatePage,
  CandidateRecord,
  ClothingItem,
  DashboardStats,
  ErrorEventPage,
  ErrorNotificationSettings,
  ErrorSource,
  JudgmentPromptPreview,
  OperationsSnapshot,
  ScoreDistribution,
  ScoreDistributionWindow,
  SearchCategoryOptions,
  SearchRecord,
  SearchRunPage,
  SearchRunResult,
  SearchUpdatePayload,
  SettingsSnapshot,
  StorageStats,
  TasteRecomputeResult,
  TasteSample,
  TasteSnapshot,
  TelegramWebhookStatus,
  VintedBrandOption,
  VintedSizesResult,
  WardrobeZipImportResult,
} from '../types';

// Shared shape for the settings PUT payload — used by both the API client and
// SettingsPage's form state, so the two never drift out of sync.
type SettingsUpdatePayload = {
  vinted_region: string;
  vinted_cookie: string;
  vinted_refresh_token: string;
  telegram_bot_token: string;
  telegram_chat_id: string;
  telegram_webhook_url: string;
  telegram_webhook_secret: string;
  judge_model_id: string | null;
  judge_fallback_model_id: string | null;
  learn_model_id: string | null;
  observation_model_id: string | null;
  vlm_grid_size: number;
  vlm_pack_multiple_listing_images: boolean;
  vlm_judge_parallel_requests: number;
  ai_judge_image_max_px: number;
  alert_threshold: number;
  scan_interval_seconds: number;
};

export type SettingsSavePayload = Omit<
  SettingsUpdatePayload,
  | 'judge_model_id'
  | 'judge_fallback_model_id'
  | 'learn_model_id'
  | 'observation_model_id'
  | 'vlm_grid_size'
  | 'vlm_pack_multiple_listing_images'
  | 'vlm_judge_parallel_requests'
  | 'ai_judge_image_max_px'
>;

// The AI Models page owns this subset of Settings. Keeping it separate from the
// full settings form prevents a model configuration save from touching Vinted,
// Telegram, or scan settings.
export type AiModelSettingsSavePayload = Pick<
  SettingsUpdatePayload,
  | 'vinted_region'
  | 'judge_model_id'
  | 'judge_fallback_model_id'
  | 'learn_model_id'
  | 'observation_model_id'
  | 'vlm_grid_size'
  | 'vlm_pack_multiple_listing_images'
  | 'vlm_judge_parallel_requests'
  | 'ai_judge_image_max_px'
>;

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '';

// Carries the HTTP status (0 = network failure) alongside a human-readable
// message so callers can branch on `status` and render the server detail.
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    detail?: ApiErrorDetail,
  ) {
    super(ApiError.describe(status, path, detail?.message));
    this.name = 'ApiError';
    this.code = detail?.code;
    this.recoveryPath = detail?.recoveryPath;
  }

  readonly code?: string;
  readonly recoveryPath?: string;

  private static describe(status: number, path: string, detail?: string): string {
    if (detail) return detail;
    if (status === 0) return 'Network error - is the backend reachable?';
    if (status === 401 || status === 403) return 'Not authorized - check your credentials.';
    if (status === 404) return `Not found: ${path}`;
    if (status >= 500) return `Server error (${status}) - please try again.`;
    return `Request failed (${status}).`;
  }
}

type ApiErrorDetail = {
  message?: string;
  code?: string;
  recoveryPath?: string;
};

// FastAPI errors come back as { detail: string } or { detail: [{ msg, ... }] }.
async function extractDetail(response: Response): Promise<ApiErrorDetail | undefined> {
  try {
    const data = await response.clone().json();
    if (typeof data?.detail === 'string') return { message: data.detail };
    if (Array.isArray(data?.detail)) {
      const messages = data.detail
        .map((item: unknown) => {
          if (typeof item === 'string') return item;
          if (typeof (item as { msg?: unknown })?.msg === 'string') return (item as { msg: string }).msg;
          return '';
        })
        .filter(Boolean);
      if (messages.length) return { message: messages.join('\n') };
    }
    if (data?.detail && typeof data.detail === 'object') {
      const detail = data.detail as {
        message?: unknown;
        code?: unknown;
        recovery_path?: unknown;
      };
      return {
        message: typeof detail.message === 'string' ? detail.message : JSON.stringify(data.detail),
        code: typeof detail.code === 'string' ? detail.code : undefined,
        recoveryPath: typeof detail.recovery_path === 'string' ? detail.recovery_path : undefined,
      };
    }
  } catch {
    const text = await response.clone().text().catch(() => '');
    if (text.trim()) return { message: text.trim() };
  }
  return undefined;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers ?? {});
  const isFormData = init?.body instanceof FormData;

  if (!isFormData && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, path);
  }

  if (!response.ok) {
    throw new ApiError(response.status, path, await extractDetail(response));
  }

  if (response.status === 204 || response.headers.get('Content-Length') === '0') {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

type BlobResponse = {
  blob: Blob;
  headers: Headers;
};

// Binary-aware variant of `request` for endpoints that return non-JSON bodies.
async function requestBlob(path: string, init?: RequestInit): Promise<BlobResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch {
    throw new ApiError(0, path);
  }
  if (!response.ok) {
    throw new ApiError(response.status, path, await extractDetail(response));
  }
  return { blob: await response.blob(), headers: response.headers };
}

function filenameFromContentDisposition(value: string | null): string | null {
  if (!value) return null;
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(value);
  return match ? decodeURIComponent(match[1]) : null;
}

export const api = {
  getDashboardStats: () => request<DashboardStats>('/api/stats/dashboard'),
  getAiCostStats: () => request<AiCostStats>('/api/stats/costs'),
  getScoreDistribution: (window: ScoreDistributionWindow) =>
    request<ScoreDistribution>(`/api/stats/score-distribution?window=${window}`),
  getStorageStats: () => request<StorageStats>('/api/storage/stats'),
  clearCandidateImageCache: () => request<CacheClearResult>('/api/storage/cache/clear', { method: 'POST' }),
  getErrors: (params: { source?: ErrorSource; limit: number; offset: number }) => {
    const search = new URLSearchParams();
    if (params.source) search.set('source', params.source);
    search.set('limit', String(params.limit));
    search.set('offset', String(params.offset));
    return request<ErrorEventPage>(`/api/errors?${search.toString()}`);
  },
  updateErrorTelegramNotifications: (enabled: boolean) =>
    request<ErrorNotificationSettings>('/api/errors/telegram-notifications', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  getSearches: () => request<SearchRecord[]>('/api/searches'),
  getSearchCategoryOptions: () => request<SearchCategoryOptions>('/api/searches/category-options'),
  updateSearch: (searchId: string, payload: SearchUpdatePayload) =>
    request<SearchRecord>(`/api/searches/${searchId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  runSearch: (searchId: string) => request<SearchRunResult>(`/api/searches/${searchId}/run`, { method: 'POST' }),
  runAllSearches: () => request<SearchRunResult[]>('/api/searches/run-all', { method: 'POST' }),
  cancelSearch: (searchId: string) => request<void>(`/api/searches/${searchId}/cancel`, { method: 'POST' }),
  retryDelivery: (candidateId: string) =>
    request<void>(`/api/candidates/${candidateId}/retry-delivery`, { method: 'POST' }),
  toggleSearch: (searchId: string) => request<SearchRecord>(`/api/searches/${searchId}/toggle`, { method: 'POST' }),
  getTaste: () => request<TasteSnapshot>('/api/taste'),
  getJudgmentPrompt: (clothingItem: ClothingItem) =>
    request<JudgmentPromptPreview>(`/api/taste/judgment-prompt?clothing_item=${clothingItem}`),
  recomputeTaste: () => request<TasteRecomputeResult>('/api/taste/recompute', { method: 'POST' }),
  cancelTasteRecompute: () => request<TasteSnapshot>('/api/taste/recompute/cancel', { method: 'POST' }),
  uploadWardrobeImage: (file: File, note: string, clothingItem: ClothingItem) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('clothing_item', clothingItem);
    formData.append('note', note);

    return request<TasteSample>('/api/taste/wardrobe', {
      method: 'POST',
      body: formData,
    });
  },
  importWardrobeZip: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return request<WardrobeZipImportResult>('/api/taste/wardrobe/import-zip', { method: 'POST', body: fd });
  },
  addTasteOffer: (payload: {
    kind: 'offer_like' | 'offer_dislike';
    clothing_item: ClothingItem;
    vinted_url?: string;
    note: string;
  }) =>
    request<TasteSample>('/api/taste/offers/from-url', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateTasteSample: (
    sampleId: string,
    payload: { note?: string; kind?: 'wardrobe' | 'offer_like' | 'offer_dislike' | 'offer_note'; clothing_item?: ClothingItem },
  ) =>
    request<TasteSample>(`/api/taste/samples/${sampleId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteTasteSample: (sampleId: string) => request<TasteSample>(`/api/taste/samples/${sampleId}`, { method: 'DELETE' }),
  updateTasteManualNote: (text: string) =>
    request<TasteSnapshot>('/api/taste/manual-note', {
      method: 'PUT',
      body: JSON.stringify({ text }),
    }),
  getCandidates: (params: {
    clothing_item?: string;
    stage?: string;
    decision?: string;
    feedback?: string;
    delivery_status?: string;
    window?: string;
    sort?: string;
    limit: number;
    offset: number;
  }) => {
    const search = new URLSearchParams();
    if (params.clothing_item && params.clothing_item !== 'all') search.set('clothing_item', params.clothing_item);
    if (params.stage && params.stage !== 'all') search.set('stage', params.stage);
    if (params.decision && params.decision !== 'all') search.set('decision', params.decision);
    if (params.feedback && params.feedback !== 'all') search.set('feedback', params.feedback);
    if (params.delivery_status && params.delivery_status !== 'all') search.set('delivery_status', params.delivery_status);
    if (params.window) search.set('window', params.window);
    if (params.sort && params.sort !== 'score_desc') search.set('sort', params.sort);
    search.set('limit', String(params.limit));
    search.set('offset', String(params.offset));
    return request<CandidatePage>(`/api/candidates?${search.toString()}`);
  },
  sendFeedback: (candidateId: string, verdict: 'like' | 'dislike') =>
    request<CandidateRecord>(`/api/candidates/${candidateId}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ verdict, skip_if_unchanged: true }),
    }),
  sendFeedbackWithComment: (candidateId: string, verdict: 'like' | 'dislike', comment: string) =>
    request<CandidateRecord>(`/api/candidates/${candidateId}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ verdict, comment, skip_if_unchanged: true }),
    }),
  sendCandidateNote: (candidateId: string, comment: string) =>
    request<CandidateRecord>(`/api/candidates/${candidateId}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ comment, skip_if_unchanged: true }),
    }),
  getSettings: () => request<SettingsSnapshot>('/api/settings'),
  getBlockedBrands: () => request<{ brands: string[] }>('/api/settings/blocked-brands'),
  updateBlockedBrands: (brands: string[]) =>
    request<{ brands: string[] }>('/api/settings/blocked-brands', {
      method: 'PUT',
      body: JSON.stringify({ brands }),
    }),
  searchVintedBrands: (query: string) =>
    request<VintedBrandOption[]>(`/api/vinted/brands?query=${encodeURIComponent(query)}`),
  validateCookie: (cookie: string) =>
    request<{
      region: string;
      status: 'healthy' | 'warning' | 'missing';
      last_validated_at: string | null;
      detail: string;
    }>('/api/settings/validate-cookie', {
      method: 'POST',
      body: JSON.stringify({ cookie }),
    }),
  saveSettings: (payload: SettingsSavePayload) =>
    request<SettingsSnapshot>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  saveAiModelSettings: (payload: AiModelSettingsSavePayload) =>
    request<SettingsSnapshot>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  // model_id scopes the test to one registered AiModelConfig entry
  // (the route resolves provider/base_url/model_name from the registry server-side).
  testModel: (payload: { model_id: string; prompt: string }) =>
    request<{ ok: boolean; provider: string; base_url: string | null; model: string; answer: string }>('/api/settings/test-model', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getAiModels: () => request<AiModelConfig[]>('/api/ai-models'),
  createAiModel: (payload: AiModelCreate) =>
    request<AiModelConfig>('/api/ai-models', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateAiModel: (modelId: string, payload: AiModelUpdate) =>
    request<AiModelConfig>(`/api/ai-models/${modelId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteAiModel: (modelId: string) =>
    request<void>(`/api/ai-models/${modelId}`, { method: 'DELETE' }),
  syncVintedSizes: () => request<VintedSizesResult>('/api/searches/sync-sizes', { method: 'POST' }),
  applyProfileSizesToAll: () => request<VintedSizesResult>('/api/searches/apply-profile-sizes', { method: 'POST' }),
  telegramPreview: () => request<{ preview: string }>('/api/telegram/test', { method: 'POST' }),
  sendTelegramTest: () => request<{ ok: boolean; message_id?: number; chat_id: string }>('/api/telegram/test/send', { method: 'POST' }),
  getTelegramWebhookStatus: () => request<TelegramWebhookStatus>('/api/telegram/webhook'),
  registerTelegramWebhook: (payload: { url?: string; drop_pending_updates?: boolean }) =>
    request<TelegramWebhookStatus>('/api/telegram/webhook/register', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  exportData: async (includeCache: boolean) => {
    const { blob, headers } = await requestBlob(`/api/data/export?include_cache=${includeCache ? 'true' : 'false'}`);
    // Trigger a browser save-as download via a temporary object URL.
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filenameFromContentDisposition(headers.get('Content-Disposition')) ?? 'vsniper-backup.zip';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },
  importData: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return request<BackupImportResult>('/api/data/import', { method: 'POST', body: fd });
  },
  getOperationsStatus: () => request<OperationsSnapshot>('/api/operations/status'),
  maintenancePause: (reason?: string) =>
    request<void>('/api/operations/maintenance/pause', {
      method: 'POST',
      body: JSON.stringify({ reason: reason ?? '' }),
    }),
  maintenanceResume: () =>
    request<void>('/api/operations/maintenance/resume', { method: 'POST' }),
  getSearchRuns: (params: { search_id?: string; status?: string; limit?: number; offset?: number }) => {
    const search = new URLSearchParams();
    if (params.search_id) search.set('search_id', params.search_id);
    if (params.status) search.set('status', params.status);
    if (params.limit) search.set('limit', String(params.limit));
    if (params.offset) search.set('offset', String(params.offset));
    return request<SearchRunPage>(`/api/operations/search-runs?${search.toString()}`);
  },
};
