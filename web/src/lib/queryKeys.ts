// Single source of truth for TanStack Query cache keys. Using these instead of
// inline string literals stops a typo in one `invalidateQueries` call from
// silently failing to refresh a cache.
export const queryKeys = {
  stats: ['stats'] as const,
  costs: ['ai-cost-stats'] as const,
  scoreDistribution: (window: string) => ['score-distribution', window] as const,
  storage: ['storage'] as const,
  searches: ['searches'] as const,
  searchCategoryOptions: ['search-category-options'] as const,
  taste: ['taste'] as const,
  judgmentPrompt: (clothingItem: string) => ['judgment-prompt', clothingItem] as const,
  candidates: ['candidates'] as const,
  candidatesPage: (params: {
    clothing_item: string;
    stage: string;
    decision: string;
    feedback: string;
    delivery_status: string;
    window: string;
    sort: string;
    offset: number;
    limit: number;
  }) =>
    [
      'candidates',
      params.clothing_item,
      params.stage,
      params.decision,
      params.feedback,
      params.delivery_status,
      params.window,
      params.sort,
      params.offset,
      params.limit,
    ] as const,
  settings: ['settings'] as const,
  blockedBrands: ['blocked-brands'] as const,
  vintedBrandSearch: (query: string) => ['vinted-brand-search', query] as const,
  aiModels: ['ai-models'] as const,
  telegramPreview: ['telegram-preview'] as const,
  telegramWebhook: ['telegram-webhook'] as const,
  data: ['data'] as const,
  operations: ['operations'] as const,
  searchRuns: (params: { search_id?: string; status?: string }) =>
    ['search-runs', params.search_id ?? '', params.status ?? ''] as const,
};
