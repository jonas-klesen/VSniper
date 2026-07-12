import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../lib/api';
import { modelLabel } from '../lib/aiModels';
import { clothingItemLabel } from '../lib/clothingItems';
import { formatBerlinDateTime } from '../lib/datetime';
import { decodeJwtExpiry, decodeRefreshTokenExpiry, formatCookieExpiry } from '../lib/jwt';
import { queryKeys } from '../lib/queryKeys';
import { ErrorText } from '../components/ErrorText';
import { ScoreDistributionChart } from '../components/ScoreDistributionChart';
import type { SearchRunRecord } from '../types';

import {
  Activity,
  AlertTriangle,
  Bell,
  Clock,
  Download,
  HardDrive,
  Heart,
  PauseCircle,
  PlayCircle,
  RefreshCw,
  Server,
  Upload,
  Zap,
} from 'lucide-react';

function formatDuration(ms: number | null): string {
  if (ms === null) return '-';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return '-';
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function RunStatusPill({ status }: { status: string }) {
  const cls =
    status === 'completed' ? 'healthy' :
    status === 'running' ? 'warning' :
    status === 'failed' ? 'missing' : '';
  return <span className={`pill ${cls}`}>{status}</span>;
}

function RunDetailsRow({ run }: { run: SearchRunRecord }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <>
      <tr>
        <td>{run.search_id}</td>
        <td>{run.mode}</td>
        <td>{run.trigger}</td>
        <td><RunStatusPill status={run.status} /></td>
        <td>{run.fetched_count}</td>
        <td>{run.new_judged_count} this run / {run.judged_count} total</td>
        <td>{run.alert_count}</td>
        <td>{formatDuration(run.duration_ms)}</td>
        <td>${run.cost_usd.toFixed(4)}</td>
        <td>{formatBerlinDateTime(run.started_at, '-')}</td>
        <td>
          <button className="secondary" style={{ padding: '0.2rem 0.5rem', fontSize: '0.75rem' }} onClick={() => setExpanded(!expanded)}>
            {expanded ? 'Hide' : 'Details'}
          </button>
        </td>
      </tr>
      {expanded ? (
        <tr>
          <td colSpan={11} className="compact-expanded">
            <ul className="detail-list">
              {run.judge_provider ? <li><strong>Judge provider:</strong> {run.judge_provider}</li> : null}
              {run.judge_model ? <li><strong>Judge model:</strong> {run.judge_model}</li> : null}
              {run.fallback_used ? <li><strong>Fallback used:</strong> yes</li> : null}
              {run.vinted_status ? <li><strong>Vinted status:</strong> {run.vinted_status}</li> : null}
              {run.vinted_detail ? <li><strong>Vinted detail:</strong> {run.vinted_detail}</li> : null}
              <li><strong>Input tokens:</strong> {run.input_tokens.toLocaleString()}</li>
              <li><strong>Output tokens:</strong> {run.output_tokens.toLocaleString()}</li>
              <li><strong>Queued deliveries:</strong> {run.queued_delivery_count}</li>
              {run.finished_at ? <li><strong>Finished:</strong> {formatBerlinDateTime(run.finished_at, '-')}</li> : null}
              {run.error ? <li><strong>Error:</strong> <code>{run.error}</code></li> : null}
              {run.failures_by_reason ? (
                <li>
                  <strong>Failure reasons:</strong>
                  <ul>
                    {Object.entries(run.failures_by_reason).map(([reason, count]) => (
                      <li key={reason}>{reason}: {count}</li>
                    ))}
                  </ul>
                </li>
              ) : null}
            </ul>
          </td>
        </tr>
      ) : null}
    </>
  );
}

function DataManagementPanel() {
  const queryClient = useQueryClient();
  const [includeCache, setIncludeCache] = useState(false);

  const exportMutation = useMutation({
    mutationFn: () => api.exportData(includeCache),
  });

  const importMutation = useMutation({
    mutationFn: (file: File) => api.importData(file),
    onSuccess: () => queryClient.invalidateQueries(),
  });

  const onImportFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const confirmed = window.confirm(
      `Importing "${file.name}" will OVERWRITE all current data (database, uploads, and cache).\n\n` +
        'A timestamped backup of the previous state is kept under storage/.\n\n' +
        'Continue?',
    );
    if (!confirmed) {
      e.target.value = '';
      return;
    }
    importMutation.mutate(file);
    e.target.value = '';
  };

  return (
    <div className="stack compact">
      <label className="checkbox-label">
        <input type="checkbox" checked={includeCache} onChange={(e) => setIncludeCache(e.target.checked)} />
        Include cached images
      </label>
      <div className="button-row">
        <button onClick={() => exportMutation.mutate()} disabled={exportMutation.isPending || importMutation.isPending}>
          <Download size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
          {exportMutation.isPending ? 'Exporting...' : 'Export'}
        </button>
        <label className="button" style={{ position: 'relative', cursor: 'pointer' }}>
          <Upload size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
          {importMutation.isPending ? 'Importing...' : 'Import'}
          <input type="file" accept=".zip,application/zip" style={{ position: 'absolute', inset: 0, opacity: 0, cursor: 'pointer' }} onChange={onImportFile} disabled={exportMutation.isPending || importMutation.isPending} />
        </label>
      </div>
      <ErrorText error={exportMutation.error} prefix="Export failed" />
      <ErrorText error={importMutation.error} prefix="Import failed" />
      {exportMutation.isSuccess && <p className="muted">Backup downloaded.</p>}
      {importMutation.isSuccess && (
        <p className="muted">
          Imported {importMutation.data.restored_files} file(s).
          {importMutation.data.reloaded ? ' State reloaded.' : ' Reload failed - restart required.'}
        </p>
      )}
    </div>
  );
}

function OperationsDashboardSection() {
  const queryClient = useQueryClient();
  const opsQuery = useQuery({
    queryKey: queryKeys.operations,
    queryFn: api.getOperationsStatus,
    refetchInterval: 10_000,
  });
  const [runSearchFilter, setRunSearchFilter] = useState('');
  const [runStatusFilter, setRunStatusFilter] = useState('');
  const runsQuery = useQuery({
    queryKey: queryKeys.searchRuns({ search_id: runSearchFilter, status: runStatusFilter }),
    queryFn: () => api.getSearchRuns({ search_id: runSearchFilter || undefined, status: runStatusFilter || undefined, limit: 50 }),
    refetchInterval: 15_000,
  });

  const pauseMutation = useMutation({
    mutationFn: (reason: string) => api.maintenancePause(reason),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.operations }),
  });

  const resumeMutation = useMutation({
    mutationFn: () => api.maintenanceResume(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.operations }),
  });

  const cancelMutation = useMutation({
    mutationFn: (searchId: string) => api.cancelSearch(searchId),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.operations });
      queryClient.invalidateQueries({ queryKey: queryKeys.searchRuns({ search_id: runSearchFilter, status: runStatusFilter }) });
    },
  });

  if (opsQuery.isLoading) return <p>Loading operations status...</p>;
  if (opsQuery.isError || !opsQuery.data) return <ErrorText error={opsQuery.error ?? 'Could not load operations status.'} />;

  const ops = opsQuery.data;
  const isPaused = ops.maintenance_mode !== 'idle';
  const runs = runsQuery.data?.items ?? [];

  return (
    <>
      <div className="card-grid">
        <article className="card stat-card">
          <span><Server size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Maintenance</span>
          <strong>{ops.maintenance_mode}</strong>
          <span className="muted">{ops.maintenance_reason || (isPaused ? 'Paused' : 'Idle')}</span>
        </article>

        <article className="card stat-card">
          <span><Heart size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Worker heartbeat</span>
          <strong>{ops.worker_heartbeat ? formatBerlinDateTime(ops.worker_heartbeat.last_heartbeat_at, 'never') : 'No worker'}</strong>
          <span className="muted">
            {ops.worker_heartbeat ? `Cycle ${ops.worker_heartbeat.cycle_count} - ${ops.worker_heartbeat.phase}` : 'Not running'}
          </span>
        </article>

        <article className="card stat-card">
          <span><Activity size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Active tasks</span>
          <strong>{ops.active_worker_tasks.length}</strong>
          <span className="muted">{ops.active_worker_tasks.map((t) => t.operation).join(', ') || 'None'}</span>
        </article>

        <article className="card stat-card">
          <span><RefreshCw size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Taste recompute</span>
          <strong>{ops.recompute_status}</strong>
          <span className="muted">{ops.recompute_status === 'running' ? 'In progress' : 'Ready'}</span>
        </article>

        <article className="card stat-card">
          <span><Bell size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Queued deliveries</span>
          <strong>{ops.delivery_summary.pending + ops.delivery_summary.processing}</strong>
          <span className="muted">{ops.delivery_summary.sent} sent total</span>
        </article>

        <article className="card stat-card">
          <span><AlertTriangle size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Failed deliveries</span>
          <strong className={ops.delivery_summary.failed > 0 ? 'text-warning' : ''}>{ops.delivery_summary.failed}</strong>
          <span className="muted">{ops.delivery_summary.failed > 0 ? 'Needs attention' : 'All clear'}</span>
        </article>
      </div>

      <article className="card">
        <h3><PauseCircle size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Maintenance control</h3>
        <p className="muted">
          Pausing stops worker scans and blocks manual scan triggers. Import maintenance is managed automatically during data imports.
        </p>
        <div className="button-row" style={{ marginTop: '0.75rem' }}>
          {isPaused ? (
            <button onClick={() => resumeMutation.mutate()} disabled={resumeMutation.isPending}>
              <PlayCircle size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
              {resumeMutation.isPending ? 'Resuming...' : 'Resume'}
            </button>
          ) : (
            <button className="secondary" onClick={() => pauseMutation.mutate('Manual pause from Dashboard')} disabled={pauseMutation.isPending}>
              <PauseCircle size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />
              {pauseMutation.isPending ? 'Pausing...' : 'Pause worker'}
            </button>
          )}
          {ops.maintenance_started_at && isPaused ? (
            <span className="muted">Paused since {formatBerlinDateTime(ops.maintenance_started_at, '-')}</span>
          ) : null}
        </div>
        <ErrorText error={pauseMutation.error} prefix="Pause failed" />
        <ErrorText error={resumeMutation.error} prefix="Resume failed" />
      </article>

      {ops.search_claims.length > 0 ? (
        <article className="card">
          <h3><Zap size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Active scan claims</h3>
          <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr className="muted">
                <th>Search</th>
                <th>Bucket</th>
                <th>Status</th>
                <th>Claim age</th>
                <th>Last run</th>
                <th></th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {ops.search_claims.map((claim) => (
                <tr key={claim.search_id}>
                  <td>{claim.search_name}</td>
                  <td>{clothingItemLabel(claim.clothing_item)}</td>
                  <td><RunStatusPill status={claim.run_status} /></td>
                  <td>{formatAge(claim.claim_age_seconds)}</td>
                  <td>{formatBerlinDateTime(claim.last_run_at, 'never')}</td>
                  <td>{claim.is_stale ? <span className="pill missing">stale</span> : null}</td>
                  <td>
                    {claim.run_status === 'running' ? (
                      <button
                        className="secondary"
                        onClick={() => cancelMutation.mutate(claim.search_id)}
                        disabled={cancelMutation.isPending && cancelMutation.variables === claim.search_id}
                      >
                        {cancelMutation.isPending && cancelMutation.variables === claim.search_id ? 'Cancelling...' : 'Cancel'}
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          <ErrorText error={cancelMutation.error} prefix="Cancel failed" />
        </article>
      ) : null}

      {ops.delivery_summary.latest_failures.length > 0 ? (
        <article className="card">
          <h3><AlertTriangle size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Recent delivery failures</h3>
          <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr className="muted">
                <th>Candidate</th>
                <th>Bucket</th>
                <th>Attempts</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {ops.delivery_summary.latest_failures.map((c) => (
                <tr key={c.id}>
                  <td>{c.title}</td>
                  <td>{clothingItemLabel(c.clothing_item)}</td>
                  <td>{c.telegram_delivery_attempt_count}</td>
                  <td className="muted">{c.telegram_delivery_last_error ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </article>
      ) : null}

      <article className="card">
        <h3><Clock size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Recent scan runs</h3>
        <div className="button-row compact-actions" style={{ marginBottom: '0.75rem' }}>
          <label className="inline-field">
            Search
            <select value={runSearchFilter} onChange={(e) => setRunSearchFilter(e.target.value)}>
              <option value="">All</option>
              {ops.search_claims.map((c) => (
                <option key={c.search_id} value={c.search_id}>{c.search_name}</option>
              ))}
            </select>
          </label>
          <label className="inline-field">
            Status
            <select value={runStatusFilter} onChange={(e) => setRunStatusFilter(e.target.value)}>
              <option value="">All</option>
              <option value="completed">Completed</option>
              <option value="running">Running</option>
              <option value="failed">Failed</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </label>
          <span className="muted">{runsQuery.data?.total ?? 0} runs</span>
        </div>
        {runs.length > 0 ? (
          <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr className="muted">
                <th>Search</th>
                <th>Mode</th>
                <th>Trigger</th>
                <th>Status</th>
                <th>Fetched</th>
                <th>Judged</th>
                <th>Alerts</th>
                <th>Duration</th>
                <th>Cost</th>
                <th>Started</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => <RunDetailsRow key={run.id} run={run} />)}
            </tbody>
          </table>
          </div>
        ) : (
          <p className="muted">No scan runs recorded yet.</p>
        )}
      </article>

      <article className="card">
        <h3><HardDrive size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Data management</h3>
        <p className="muted">Export or import full application state. Import pauses workers automatically.</p>
        <DataManagementPanel />
      </article>
    </>
  );
}

export function DashboardPage() {
  const statsQuery = useQuery({ queryKey: queryKeys.stats, queryFn: api.getDashboardStats, refetchInterval: 30_000 });
  const settingsQuery = useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });
  const tasteQuery = useQuery({ queryKey: queryKeys.taste, queryFn: api.getTaste });
  const telegramConfigured = settingsQuery.data?.telegram_configured ?? false;
  const webhookQuery = useQuery({
    queryKey: queryKeys.telegramWebhook,
    queryFn: api.getTelegramWebhookStatus,
    enabled: telegramConfigured,
  });

  if (statsQuery.isLoading || settingsQuery.isLoading) {
    return <p>Loading dashboard…</p>;
  }

  if (statsQuery.isError || settingsQuery.isError || !statsQuery.data || !settingsQuery.data) {
    return <ErrorText error={statsQuery.error ?? settingsQuery.error ?? 'Could not load dashboard data.'} />;
  }

  const stats = statsQuery.data;
  const settings = settingsQuery.data;

  // Access/refresh token expiry from the stored cookie (mirrors the Settings page).
  const cookieExpiry = decodeJwtExpiry(settings.vinted_cookie);
  const refreshExpiry =
    decodeRefreshTokenExpiry(settings.vinted_cookie) ||
    decodeRefreshTokenExpiry(settings.vinted_refresh_token ? `refresh_token_web=${settings.vinted_refresh_token}` : '');
  const now = new Date();

  // Recompute is "due" when training data changed and no run is currently in flight.
  const dirty = tasteQuery.data?.dirty_counts;
  const recomputeStatus = tasteQuery.data?.recompute_state?.status;
  const recomputeDue =
    !!dirty &&
    recomputeStatus !== 'running' &&
    (dirty.new_or_changed_samples > 0 || dirty.manual_note_changed);

  const webhook = webhookQuery.data;
  const webhookPill = !telegramConfigured
    ? null
    : !webhook
      ? { className: 'warning', text: 'webhook unknown' }
      : webhook.is_registered && webhook.matches_configured_url
        ? { className: 'healthy', text: 'webhook registered' }
        : webhook.is_registered
          ? { className: 'warning', text: 'webhook URL mismatch' }
          : { className: 'missing', text: 'webhook not registered' };

  return (
    <section>
      <div className="page-header">
        <div>
          <p className="eyebrow">Overview</p>
          <h2>Dashboard</h2>
        </div>
        <span className={`pill ${settings.session_health.status}`}>{settings.session_health.status}</span>
      </div>

      {recomputeDue && (
        <Link to="/my-taste" className="card banner-warning" style={{ display: 'block', textDecoration: 'none' }}>
          <strong>Taste recompute due</strong> — training data changed since the last run.{' '}
          {dirty?.new_or_changed_samples ? `${dirty.new_or_changed_samples} sample(s) changed. ` : ''}
          Go to My Taste to recompute →
        </Link>
      )}

      <div className="card-grid">
        <article className="card stat-card"><span>Active searches</span><strong>{stats.active_searches}</strong></article>
        <Link to="/candidates" className="card stat-card"><span>Candidates today</span><strong>{stats.candidates_today}</strong></Link>
        <Link to="/candidates?feedback=like" className="card stat-card"><span>Likes</span><strong>{stats.likes}</strong></Link>
        <Link to="/candidates?feedback=dislike" className="card stat-card"><span>Dislikes</span><strong>{stats.dislikes}</strong></Link>
        <Link to="/candidates?decision=alert" className="card stat-card"><span>Avg alert score</span><strong>{stats.avg_alert_score.toFixed(2)}</strong></Link>
        <article className="card stat-card"><span>Pending deliveries</span><strong>{stats.pending_deliveries}</strong></article>
        <Link to="/candidates?delivery_status=failed" className={`card stat-card${stats.failed_deliveries > 0 ? ' stat-card-warning' : ''}`}><span>Failed deliveries</span><strong>{stats.failed_deliveries}</strong></Link>
        <article className="card stat-card"><span>Last successful scan</span><strong>{formatBerlinDateTime(stats.last_successful_scan_at, 'not yet')}</strong></article>
      </div>
      <div className="card two-column">
        <div>
          <h3>Current runtime</h3>
          <ul className="detail-list">
            <li><strong>Region:</strong> {settings.vinted_region}</li>
            <li><strong>Judge model:</strong> {modelLabel(settings.models, settings.judge_model_id)}</li>
            <li><strong>Judge fallback model:</strong> {modelLabel(settings.models, settings.judge_fallback_model_id)}</li>
            <li><strong>Learn model:</strong> {modelLabel(settings.models, settings.learn_model_id)}</li>
            <li><strong>Observation model:</strong> {modelLabel(settings.models, settings.observation_model_id)}</li>
            <li><strong>Vinted credentials:</strong> {settings.vinted_configured ? 'present' : 'missing'}</li>
            <li><strong>Telegram credentials:</strong> {settings.telegram_configured ? 'present' : 'missing'}</li>
            <li><strong>Judge:</strong> {settings.judge_configured ? 'ready' : 'missing config'}</li>
            <li><strong>Learning:</strong> {settings.learning_configured ? 'OpenAI ready' : 'OpenAI missing'}</li>
          </ul>
        </div>
        <div>
          <h3>Session health</h3>
          <p>{settings.session_health.detail}</p>
          <p className="muted">Last validated: {formatBerlinDateTime(settings.session_health.last_validated_at, 'not validated yet')}</p>
          <ul className="detail-list">
            <li>
              <strong>Access token:</strong>{' '}
              <span style={cookieExpiry && cookieExpiry <= now ? { color: '#f55' } : undefined}>
                {cookieExpiry ? formatCookieExpiry(cookieExpiry) : 'unknown'}
              </span>
            </li>
            <li>
              <strong>Refresh token:</strong>{' '}
              <span style={refreshExpiry && refreshExpiry <= now ? { color: '#f55' } : undefined}>
                {refreshExpiry ? formatCookieExpiry(refreshExpiry) : 'unknown'}
              </span>
            </li>
            {webhookPill && (
              <li>
                <strong>Telegram webhook:</strong>{' '}
                <span className={`pill ${webhookPill.className}`}>{webhookPill.text}</span>
              </li>
            )}
          </ul>
          <p className="muted">Preview scans stay local to the dashboard. The worker now uses a separate live scan path that can queue alert deliveries.</p>
        </div>
      </div>

      <ScoreDistributionChart />

      <OperationsDashboardSection />
    </section>
  );
}
