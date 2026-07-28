import { useEffect } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';

import { ErrorText } from '../components/ErrorText';
import { api } from '../lib/api';
import { formatBerlinDateTime } from '../lib/datetime';
import { queryKeys } from '../lib/queryKeys';
import type {
  ErrorEventRecord,
  ErrorSource,
  ErrorTelegramNotificationStatus,
} from '../types';

const PAGE_SIZE = 50;
const SOURCES: Array<{ value: ErrorSource | ''; label: string }> = [
  { value: '', label: 'All sources' },
  { value: 'api', label: 'API' },
  { value: 'search', label: 'Searches' },
  { value: 'candidate_judgment', label: 'Candidate judging' },
  { value: 'taste_recompute', label: 'Taste recompute' },
  { value: 'telegram', label: 'Telegram' },
  { value: 'worker', label: 'Worker' },
];
const SOURCE_VALUES = new Set<string>(SOURCES.map((source) => source.value));

function notificationLabel(status: ErrorTelegramNotificationStatus): string {
  if (status === 'not_requested') return 'Not requested';
  if (status === 'pending') return 'Waiting';
  if (status === 'processing') return 'Sending';
  if (status === 'sent') return 'Sent';
  return 'Failed';
}

function notificationClass(status: ErrorTelegramNotificationStatus): string {
  if (status === 'sent') return 'healthy';
  if (status === 'failed') return 'missing';
  if (status === 'pending' || status === 'processing') return 'warning';
  return '';
}

function ErrorEntry({ event }: { event: ErrorEventRecord }) {
  const hasDetails = Object.keys(event.details).length > 0;
  return (
    <article className="card error-event">
      <div className="error-event__header">
        <div>
          <div className="tag-row">
            <span className="pill missing">{event.source.replace('_', ' ')}</span>
            <span className="pill">{event.operation}</span>
            <span className={`pill ${notificationClass(event.telegram_notification_status)}`}>
              Telegram: {notificationLabel(event.telegram_notification_status)}
            </span>
          </div>
          <h3>{event.summary}</h3>
        </div>
        <time className="muted" dateTime={event.occurred_at}>
          {formatBerlinDateTime(event.occurred_at, event.occurred_at)}
        </time>
      </div>

      <p className="error-event__message">{event.message}</p>

      <div className="error-event__meta muted">
        <span>#{event.id}</span>
        {event.exception_type ? <span>{event.exception_type}</span> : null}
        {event.related_entity_type && event.related_entity_id ? (
          <span>{event.related_entity_type}: {event.related_entity_id}</span>
        ) : null}
        {event.telegram_notification_attempt_count > 0 ? (
          <span>{event.telegram_notification_attempt_count} Telegram attempt{event.telegram_notification_attempt_count === 1 ? '' : 's'}</span>
        ) : null}
      </div>

      {event.telegram_notification_last_error ? (
        <p className="error-text">Telegram notification failed: {event.telegram_notification_last_error}</p>
      ) : null}

      {hasDetails ? (
        <details className="details-block">
          <summary>Technical details</summary>
          <pre className="error-event__details">{JSON.stringify(event.details, null, 2)}</pre>
        </details>
      ) : null}
    </article>
  );
}

export function ErrorsPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const sourceParam = searchParams.get('source') ?? '';
  const source = SOURCE_VALUES.has(sourceParam) ? (sourceParam as ErrorSource | '') : '';
  const rawPage = Number.parseInt(searchParams.get('page') ?? '0', 10);
  const page = Math.max(0, Number.isFinite(rawPage) ? rawPage : 0);
  const offset = page * PAGE_SIZE;
  const keyParams = { source, offset, limit: PAGE_SIZE };

  const errorsQuery = useQuery({
    queryKey: queryKeys.errors(keyParams),
    queryFn: () => api.getErrors({ source: source || undefined, offset, limit: PAGE_SIZE }),
    refetchInterval: 10_000,
    placeholderData: (previous) => previous,
  });

  const toggleMutation = useMutation({
    mutationFn: api.updateErrorTelegramNotifications,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['errors'] }),
  });

  const setFilter = (nextSource: string) => {
    const next = new URLSearchParams(searchParams);
    if (nextSource) next.set('source', nextSource);
    else next.delete('source');
    next.delete('page');
    setSearchParams(next, { replace: true });
  };

  const setPage = (nextPage: number) => {
    const next = new URLSearchParams(searchParams);
    if (nextPage > 0) next.set('page', String(nextPage));
    else next.delete('page');
    setSearchParams(next, { replace: true });
  };

  const total = errorsQuery.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  useEffect(() => {
    if (errorsQuery.data && page >= totalPages) setPage(totalPages - 1);
  }, [errorsQuery.data, page, totalPages]);

  return (
    <section>
      <div className="page-header">
        <div>
          <p className="eyebrow">Operational log</p>
          <h2>Errors</h2>
          <p className="muted">Terminal failures from searches, integrations, deliveries, API requests, and worker jobs.</p>
        </div>
      </div>

      <article className="card error-notification-setting">
        <div>
          <h3>Telegram error notifications</h3>
          <p className="muted">
            Send each new error to the configured Telegram chat. Delivery is retried three times without creating recursive errors.
          </p>
          {errorsQuery.data && !errorsQuery.data.telegram_configured ? (
            <p className="text-warning">Configure a Telegram bot token and chat ID in Settings before enabling this.</p>
          ) : null}
        </div>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={errorsQuery.data?.telegram_notifications_enabled ?? false}
            disabled={
              toggleMutation.isPending
              || !errorsQuery.data
              || (!errorsQuery.data.telegram_configured && !errorsQuery.data.telegram_notifications_enabled)
            }
            onChange={(event) => toggleMutation.mutate(event.target.checked)}
          />
          <span>{errorsQuery.data?.telegram_notifications_enabled ? 'Enabled' : 'Disabled'}</span>
        </label>
      </article>
      <ErrorText error={toggleMutation.error} prefix="Could not update Telegram error notifications" />

      <div className="error-toolbar">
        <label>
          Source
          <select value={source} onChange={(event) => setFilter(event.target.value)}>
            {SOURCES.map((option) => (
              <option key={option.value || 'all'} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <span className="muted">{total} error{total === 1 ? '' : 's'}</span>
      </div>

      {errorsQuery.isLoading ? <p>Loading errors...</p> : null}
      {errorsQuery.isError ? <ErrorText error={errorsQuery.error} prefix="Could not load errors" /> : null}
      {errorsQuery.data?.items.length === 0 ? (
        <div className="card"><p className="muted">No matching operational errors have been recorded.</p></div>
      ) : null}
      <div className="error-event-list">
        {errorsQuery.data?.items.map((event) => <ErrorEntry event={event} key={event.id} />)}
      </div>

      {errorsQuery.data && totalPages > 1 ? (
        <div className="button-row pagination-row">
          <button className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
          <span className="muted">Page {page + 1} of {totalPages}</span>
          <button className="secondary" disabled={page >= totalPages - 1} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      ) : null}
    </section>
  );
}
