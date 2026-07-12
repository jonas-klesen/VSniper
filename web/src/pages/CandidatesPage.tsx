import { useEffect, useState } from 'react';

import { useMutation, useQuery, useQueryClient, type UseMutationResult } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';

import { api } from '../lib/api';
import { clothingItemLabel, clothingItems } from '../lib/clothingItems';
import { queryKeys } from '../lib/queryKeys';
import { ErrorText } from '../components/ErrorText';
import type { CandidatePage as CandidatePageData, CandidateRecord, ClothingItem } from '../types';

import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  RotateCw,
  ThumbsDown,
  ThumbsUp,
} from 'lucide-react';

type StageFilter = 'all' | 'vlm_judged' | 'failed';
type ClothingItemFilter = ClothingItem | 'all';
type ViewMode = 'cards' | 'compact';
type TimeWindow = '1h' | '6h' | '12h' | '1d' | '7d' | '30d';

const STAGE_LABELS: Record<StageFilter, string> = {
  all: 'All',
  vlm_judged: 'VLM judged',
  failed: 'Failed',
};

const DECISION_OPTIONS: Record<string, string> = {
  all: 'Any decision',
  alert: 'Alert',
  review: 'Review',
  discard: 'Discard',
};

const FEEDBACK_OPTIONS: Record<string, string> = {
  all: 'Any feedback',
  unknown: 'No feedback',
  like: 'Liked',
  dislike: 'Disliked',
};

const SORT_OPTIONS: Record<string, string> = {
  newest: 'Newest first',
  oldest: 'Oldest first',
  price_asc: 'Price up',
  price_desc: 'Price down',
  score_desc: 'Score down',
  score_asc: 'Score up',
};

const TIME_WINDOW_OPTIONS: Record<TimeWindow, string> = {
  '1h': 'Last hour',
  '6h': 'Last 6 hours',
  '12h': 'Last 12 hours',
  '1d': 'Last day',
  '7d': 'Last 7 days',
  '30d': 'Last 30 days',
};

function formatEuroPrice(price: number): string {
  return `${Math.round(price)}€`;
}

const CLOTHING_ITEM_VALUES = new Set(clothingItems.map((item) => item.value));

function CandidateGallery({ candidate }: { candidate: CandidateRecord }) {
  const [active, setActive] = useState(0);
  const images = candidate.image_urls;
  if (images.length === 0) return null;
  const safeActive = Math.min(active, images.length - 1);
  return (
    <>
      <img className="candidate-image" src={images[safeActive]} alt={candidate.title} />
      {images.length > 1 ? (
        <div className="candidate-thumbs">
          {images.map((url, index) => (
            <button
              key={url}
              type="button"
              className={`candidate-thumb${index === safeActive ? ' active' : ''}`}
              onClick={() => setActive(index)}
              aria-label={`Image ${index + 1}`}
            >
              <img src={url} alt={`${candidate.title} thumbnail ${index + 1}`} />
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

function FeedbackControls({
  candidate,
  onSubmit,
  disabled,
}: {
  candidate: CandidateRecord;
  onSubmit: (candidateId: string, verdict: 'like' | 'dislike', comment: string) => void;
  disabled: boolean;
}) {
  const [comment, setComment] = useState(candidate.feedback_comment ?? '');

  useEffect(() => {
    setComment(candidate.feedback_comment ?? '');
  }, [candidate.feedback_comment]);

  return (
    <div className="stack compact">
      <label>
        Feedback comment
        <input
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          placeholder="What worked or missed?"
        />
      </label>
      <div className="button-row">
        <button onClick={() => onSubmit(candidate.id, 'like', comment)} disabled={disabled}>Like</button>
        <button className="secondary" onClick={() => onSubmit(candidate.id, 'dislike', comment)} disabled={disabled}>Dislike</button>
        <a href={candidate.url} target="_blank" rel="noreferrer">Open listing</a>
      </div>
    </div>
  );
}

function CandidateFeedback({
  candidate,
  feedbackMutation,
}: {
  candidate: CandidateRecord;
  feedbackMutation: UseMutationResult<
    CandidateRecord,
    Error,
    { candidateId: string; verdict: 'like' | 'dislike'; comment: string },
    unknown
  >;
}) {
  return (
    <>
      <FeedbackControls
        candidate={candidate}
        disabled={feedbackMutation.isPending && feedbackMutation.variables?.candidateId === candidate.id}
        onSubmit={(candidateId, verdict, comment) => feedbackMutation.mutate({ candidateId, verdict, comment })}
      />
      {feedbackMutation.isError && feedbackMutation.variables?.candidateId === candidate.id ? (
        <ErrorText error={feedbackMutation.error} prefix="Feedback failed" />
      ) : null}
      <p className="muted">Feedback status: {candidate.feedback}</p>
    </>
  );
}

function CompactRow({
  candidate,
  feedbackMutation,
  retryMutation,
}: {
  candidate: CandidateRecord;
  feedbackMutation: UseMutationResult<
    CandidateRecord,
    Error,
    { candidateId: string; verdict: 'like' | 'dislike'; comment: string },
    unknown
  >;
  retryMutation: UseMutationResult<void, Error, string, unknown>;
}) {
  const [expanded, setExpanded] = useState(false);
  const thumb = candidate.image_urls[0];
  const decisionCls = candidate.score_trace.decision === 'alert' ? 'healthy' : candidate.score_trace.decision === 'review' ? 'warning' : 'missing';
  const isFeedbackPending = feedbackMutation.isPending && feedbackMutation.variables?.candidateId === candidate.id;

  return (
    <>
      <tr className="compact-candidate-row">
        <td className="compact-thumb-cell">
          {thumb ? <img src={thumb} alt="" className="compact-thumb" /> : <div className="compact-thumb-placeholder" />}
        </td>
        <td>{clothingItemLabel(candidate.clothing_item)}</td>
        <td>
          <div className="compact-title">{candidate.title}</div>
          <div className="muted compact-meta">{candidate.brand} - size {candidate.size} - {formatEuroPrice(candidate.price_eur)}</div>
        </td>
        <td><span className={`pill ${decisionCls}`}>{candidate.score_trace.score_10}/100</span></td>
        <td><span className={`pill ${decisionCls}`}>{candidate.score_trace.decision}</span></td>
        <td>
          <span className={`pill ${candidate.telegram_delivery_status === 'sent' ? 'healthy' : candidate.telegram_delivery_status === 'failed' ? 'missing' : ''}`}>
            {candidate.telegram_delivery_status}
          </span>
        </td>
        <td>
          <div className="compact-actions">
            <button
              className="icon-btn"
              title="Like"
              onClick={() => feedbackMutation.mutate({ candidateId: candidate.id, verdict: 'like', comment: '' })}
              disabled={isFeedbackPending}
            >
              <ThumbsUp size={14} />
            </button>
            <button
              className="icon-btn secondary"
              title="Dislike"
              onClick={() => feedbackMutation.mutate({ candidateId: candidate.id, verdict: 'dislike', comment: '' })}
              disabled={isFeedbackPending}
            >
              <ThumbsDown size={14} />
            </button>
            {candidate.telegram_delivery_status === 'failed' ? (
              <button
                className="icon-btn secondary"
                title="Retry delivery"
                onClick={() => retryMutation.mutate(candidate.id)}
                disabled={retryMutation.isPending && retryMutation.variables === candidate.id}
              >
                <RotateCw size={14} />
              </button>
            ) : null}
            <a href={candidate.url} target="_blank" rel="noreferrer" className="icon-btn secondary" title="Open listing">
              <ExternalLink size={14} />
            </a>
            <button className="icon-btn secondary" onClick={() => setExpanded(!expanded)} title="Details">
              {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
          </div>
        </td>
      </tr>
      {expanded ? (
        <tr>
          <td colSpan={7} className="compact-expanded">
            <p>{candidate.score_trace.explanation || candidate.score_trace.summary}</p>
            {candidate.score_trace.labels.length > 0 ? (
              <div className="tag-row">
                {candidate.score_trace.labels.map((l) => <span className="tag subtle" key={l}>{l}</span>)}
              </div>
            ) : null}
            {candidate.score_trace.concerns.length > 0 ? (
              <div className="tag-row">
                {candidate.score_trace.concerns.map((c) => <span className="tag concern" key={c}>{c}</span>)}
              </div>
            ) : null}
            <ul className="detail-list">
              <li><strong>Source:</strong> {candidate.source_search_name ?? candidate.source_search_id}</li>
              <li><strong>Judge model:</strong> {candidate.score_trace.model ?? 'unknown'}</li>
              <li><strong>Feedback:</strong> {candidate.feedback}</li>
            </ul>
            <FeedbackControls
              candidate={candidate}
              disabled={isFeedbackPending}
              onSubmit={(candidateId, verdict, comment) => feedbackMutation.mutate({ candidateId, verdict, comment })}
            />
          </td>
        </tr>
      ) : null}
    </>
  );
}

const PAGE_SIZE = 50;

export function CandidatesPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedItemFilter = searchParams.get('clothing_item');
  const clothingItemFilter: ClothingItemFilter =
    requestedItemFilter && CLOTHING_ITEM_VALUES.has(requestedItemFilter as ClothingItem)
      ? (requestedItemFilter as ClothingItem)
      : 'all';
  const stageFilter = (searchParams.get('stage') ?? 'all') as StageFilter;
  const decisionFilter = searchParams.get('decision') ?? 'all';
  const feedbackFilter = searchParams.get('feedback') ?? 'unknown';
  const deliveryStatusFilter = searchParams.get('delivery_status') ?? 'all';
  const timeWindow = (searchParams.get('window') ?? '7d') as TimeWindow;
  const sort = searchParams.get('sort') ?? 'score_desc';
  const rawPage = parseInt(searchParams.get('page') ?? '0', 10);
  const page = Math.max(0, Number.isFinite(rawPage) ? rawPage : 0);
  const offset = page * PAGE_SIZE;
  const viewParam = searchParams.get('view') ?? 'cards';
  const viewMode: ViewMode = viewParam === 'compact' ? 'compact' : 'cards';

  const setParam = (key: string, value: string, defaultValue: string) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (!value || value === defaultValue) next.delete(key);
      else next.set(key, value);
      if (key !== 'page') next.delete('page');
      return next;
    }, { replace: true });
  };

  const candidatesQuery = useQuery({
    queryKey: queryKeys.candidatesPage({
      clothing_item: clothingItemFilter,
      stage: stageFilter,
      decision: decisionFilter,
      feedback: feedbackFilter,
      delivery_status: deliveryStatusFilter,
      window: timeWindow,
      sort,
      offset,
      limit: PAGE_SIZE,
    }),
    queryFn: () => api.getCandidates({
      clothing_item: clothingItemFilter,
      stage: stageFilter,
      decision: decisionFilter,
      feedback: feedbackFilter,
      delivery_status: deliveryStatusFilter,
      window: timeWindow,
      sort,
      limit: PAGE_SIZE,
      offset,
    }),
    refetchInterval: 30_000,
    placeholderData: (previous) => previous,
  });

  const feedbackMutation = useMutation({
    mutationFn: ({ candidateId, verdict, comment }: { candidateId: string; verdict: 'like' | 'dislike'; comment: string }) =>
      api.sendFeedbackWithComment(candidateId, verdict, comment),
    onMutate: async ({ candidateId, verdict, comment }) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.candidates });
      const snapshots = queryClient.getQueriesData<CandidatePageData>({ queryKey: queryKeys.candidates });
      queryClient.setQueriesData<CandidatePageData>({ queryKey: queryKeys.candidates }, (old) =>
        old
          ? { ...old, items: old.items.map((c) => (c.id === candidateId ? { ...c, feedback: verdict, feedback_comment: comment } : c)) }
          : old,
      );
      return { snapshots };
    },
    onError: (_error, _vars, context) => {
      context?.snapshots.forEach(([key, data]) => queryClient.setQueryData(key, data));
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.candidates });
      queryClient.invalidateQueries({ queryKey: queryKeys.taste });
      queryClient.invalidateQueries({ queryKey: queryKeys.stats });
    },
  });

  const retryMutation = useMutation({
    mutationFn: (candidateId: string) => api.retryDelivery(candidateId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.candidates }),
  });

  const setPage = (next: number) => setParam('page', String(Math.max(0, next)), '0');
  const total = candidatesQuery.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  useEffect(() => {
    if (candidatesQuery.data && page > totalPages - 1) setPage(totalPages - 1);
  }, [candidatesQuery.data, page, totalPages]);

  if (candidatesQuery.isLoading) return <p>Loading candidates...</p>;
  if (candidatesQuery.isError || !candidatesQuery.data) return <ErrorText error={candidatesQuery.error ?? 'Could not load candidates.'} />;

  const stageCounts = candidatesQuery.data.stage_counts;
  const itemCounts = candidatesQuery.data.item_counts;
  const visible = candidatesQuery.data.items;
  const selectClothingItem = (item: ClothingItemFilter) => setParam('clothing_item', item, 'all');
  const selectStage = (stage: StageFilter) => setParam('stage', stage, 'all');

  return (
    <section>
      <div className="page-header">
        <div>
          <p className="eyebrow">Review queue</p>
          <h2>Candidates</h2>
        </div>
        <div className="button-row compact-actions">
          <button
            className={viewMode === 'cards' ? '' : 'secondary'}
            onClick={() => setParam('view', 'cards', 'cards')}
            title="Card view"
          >
            Cards
          </button>
          <button
            className={viewMode === 'compact' ? '' : 'secondary'}
            onClick={() => setParam('view', 'compact', 'cards')}
            title="Compact table view"
          >
            Compact
          </button>
        </div>
      </div>
      <div className="candidate-search-tabs" aria-label="Candidate search tabs">
        <button
          className={clothingItemFilter === 'all' ? '' : 'secondary'}
          onClick={() => selectClothingItem('all')}
        >
          All ({itemCounts.all ?? 0})
        </button>
        {clothingItems.map((item) => (
          <button
            key={item.value}
            className={clothingItemFilter === item.value ? '' : 'secondary'}
            onClick={() => selectClothingItem(item.value)}
          >
            {item.label} ({itemCounts[item.value] ?? 0})
          </button>
        ))}
      </div>
      <div className="candidate-stage-tabs" aria-label="Candidate stage filters">
        {(Object.keys(STAGE_LABELS) as StageFilter[]).map((stage) => (
          <button
            key={stage}
            className={stageFilter === stage ? '' : 'secondary'}
            onClick={() => selectStage(stage)}
          >
            {STAGE_LABELS[stage]} ({stageCounts[stage] ?? 0})
          </button>
        ))}
      </div>
      <div className="candidate-filter-bar">
        <label className="inline-field">
          Decision
          <select value={decisionFilter} onChange={(e) => setParam('decision', e.target.value, 'all')}>
            {Object.entries(DECISION_OPTIONS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="inline-field">
          Feedback
          <select value={feedbackFilter} onChange={(e) => setParam('feedback', e.target.value, 'unknown')}>
            {Object.entries(FEEDBACK_OPTIONS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="inline-field">
          Time range
          <select value={timeWindow} onChange={(e) => setParam('window', e.target.value, '7d')}>
            {Object.entries(TIME_WINDOW_OPTIONS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="inline-field">
          Sort
          <select value={sort} onChange={(e) => setParam('sort', e.target.value, 'score_desc')}>
            {Object.entries(SORT_OPTIONS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <span className="muted">{total} match{total === 1 ? '' : 'es'}</span>
      </div>

      {viewMode === 'cards' ? (
        <div className="stack">
          {visible.map((candidate) => (
              <article className="card candidate-card" key={candidate.id}>
                <div className="candidate-card__main">
                <CandidateGallery candidate={candidate} />
                </div>

                <div className="candidate-card__details">
                  <div className="row-between">
                    <div>
                      <h3>{candidate.title}</h3>
                      <p className="muted">
                        {clothingItemLabel(candidate.clothing_item)} - {candidate.brand} - size {candidate.size} - {formatEuroPrice(candidate.price_eur)}
                      </p>
                    </div>
                    <span className={`pill ${candidate.score_trace.decision === 'alert' ? 'healthy' : candidate.score_trace.decision === 'review' ? 'warning' : 'missing'}`}>
                      {candidate.score_trace.decision} - {candidate.score_trace.score_10}/100
                    </span>
                  </div>

                  <div className="tag-row">
                    {candidate.matched_filters.map((item) => <span className="tag" key={item}>{item}</span>)}
                    {candidate.score_trace.labels.map((item) => <span className="tag subtle" key={item}>{item}</span>)}
                  </div>
                  <p>{candidate.score_trace.explanation || candidate.score_trace.summary}</p>
                  {candidate.score_trace.concerns.length ? (
                    <div className="tag-row">
                      {candidate.score_trace.concerns.map((item) => <span className="tag concern" key={item}>{item}</span>)}
                    </div>
                  ) : null}

                  <details className="details-block">
                    <summary>Technical details</summary>
                    <ul className="detail-list" style={{ marginTop: '0.65rem' }}>
                      <li><strong>Source search:</strong> {candidate.source_search_name ?? candidate.source_search_id}</li>
                      <li><strong>Region:</strong> {candidate.source_region ?? 'unknown'}</li>
                      <li><strong>Last scan mode:</strong> {candidate.last_scan_mode ?? 'unknown'}</li>
                      <li>
                        <strong>Delivery:</strong> {candidate.telegram_delivery_status} - attempts {candidate.telegram_delivery_attempt_count}
                        {candidate.telegram_delivery_status === 'failed' && candidate.telegram_delivery_last_error
                          ? ` - ${candidate.telegram_delivery_last_error}`
                          : ''}
                        {candidate.telegram_delivery_status === 'failed' ? (
                          <button
                            className="secondary small-action"
                            onClick={() => retryMutation.mutate(candidate.id)}
                            disabled={retryMutation.isPending && retryMutation.variables === candidate.id}
                          >
                            Retry delivery
                          </button>
                        ) : null}
                        {retryMutation.isError && retryMutation.variables === candidate.id ? (
                          <ErrorText error={retryMutation.error} prefix="Retry failed" />
                        ) : null}
                      </li>
                      <li><strong>Extraction:</strong> {candidate.extraction_status}{candidate.extraction_error ? ` - ${candidate.extraction_error}` : ''}</li>
                      <li><strong>Judge model:</strong> {candidate.score_trace.model ?? 'unknown'}</li>
                      <li><strong>Prompt version:</strong> {candidate.score_trace.prompt_version ?? 'unknown'}</li>
                      <li><strong>Grid position:</strong> {candidate.score_trace.grid_position ?? 'not batched'}</li>
                    </ul>
                  </details>
                  <CandidateFeedback candidate={candidate} feedbackMutation={feedbackMutation} />
                </div>
              </article>
            ))}
          {visible.length === 0 ? <p className="muted">No candidates on this page.</p> : null}
        </div>
      ) : (
        <>
          {visible.length > 0 ? (
            <div className="table-wrap">
              <table className="compact-candidates-table">
                <thead>
                  <tr className="muted">
                    <th style={{ width: '48px' }}></th>
                    <th>Bucket</th>
                    <th>Item</th>
                    <th>Score</th>
                    <th>Decision</th>
                    <th>Delivery</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((candidate) => (
                    <CompactRow
                      key={candidate.id}
                      candidate={candidate}
                      feedbackMutation={feedbackMutation}
                      retryMutation={retryMutation}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">No candidates on this page.</p>
          )}
        </>
      )}

      {total > PAGE_SIZE ? (
        <div className="button-row">
          <button className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>
            Previous
          </button>
          <span className="muted">
            Page {page + 1} of {totalPages} - {total} total
          </span>
          <button className="secondary" disabled={page + 1 >= totalPages} onClick={() => setPage(page + 1)}>
            Next
          </button>
        </div>
      ) : null}
    </section>
  );
}
