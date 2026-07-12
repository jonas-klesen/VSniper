import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../lib/api';
import { queryKeys } from '../lib/queryKeys';
import { ErrorText } from '../components/ErrorText';
import type { StorageCategoryStats } from '../types';

function fmtBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function CategoryRow({ label, cat, clearable }: { label: string; cat: StorageCategoryStats; clearable: boolean }) {
  return (
    <tr>
      <td>{label}{clearable && <span className="muted"> (clearable)</span>}</td>
      <td>{fmtBytes(cat.bytes)}</td>
      <td>{cat.file_count}</td>
    </tr>
  );
}

export function StoragePage() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: queryKeys.storage, queryFn: api.getStorageStats });

  const clearCacheMutation = useMutation({
    mutationFn: api.clearCandidateImageCache,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.storage }),
  });

  if (query.isLoading) {
    return <p>Loading storage data...</p>;
  }

  if (query.isError || !query.data) {
    return <ErrorText error={query.error ?? 'Could not load storage data.'} />;
  }

  const s = query.data;

  const onClearCache = () => {
    const confirmed = window.confirm(
      'This clears the cached candidate listing thumbnails (storage/cache/candidate-images). ' +
        "They'll be re-downloaded from Vinted as needed on future scans.\n\n" +
        'Taste offer examples, uploads, and the database are not affected.\n\n' +
        'Continue?',
    );
    if (!confirmed) return;
    clearCacheMutation.mutate();
  };

  return (
    <section>
      <div className="page-header">
        <div>
          <p className="eyebrow">Disk usage</p>
          <h2>Storage</h2>
        </div>
      </div>

      <div className="card-grid">
        <article className="card stat-card">
          <span>Total</span>
          <strong>{fmtBytes(s.total_bytes)}</strong>
        </article>
        <article className="card stat-card">
          <span>Database</span>
          <strong>{fmtBytes(s.database.bytes)}</strong>
          <span className="muted">{s.database.file_count} file{s.database.file_count !== 1 ? 's' : ''}</span>
        </article>
        <article className="card stat-card">
          <span>Uploads</span>
          <strong>{fmtBytes(s.uploads.bytes)}</strong>
          <span className="muted">{s.uploads.file_count} file{s.uploads.file_count !== 1 ? 's' : ''}</span>
        </article>
        <article className="card stat-card">
          <span>Feedback assets</span>
          <strong>{fmtBytes(s.feedback_assets.bytes)}</strong>
          <span className="muted">{s.feedback_assets.file_count} file{s.feedback_assets.file_count !== 1 ? 's' : ''}</span>
        </article>
        <article className="card stat-card">
          <span>Cache</span>
          <strong>
            {fmtBytes(s.cache_candidate_images.bytes + s.cache_taste_offers.bytes + s.cache_other.bytes)}
          </strong>
          <span className="muted">
            {s.cache_candidate_images.file_count + s.cache_taste_offers.file_count + s.cache_other.file_count} file
            {s.cache_candidate_images.file_count + s.cache_taste_offers.file_count + s.cache_other.file_count !== 1
              ? 's'
              : ''}
          </span>
        </article>
      </div>

      <div className="card">
        <h3>Cache breakdown</h3>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr className="muted">
                <th>Category</th>
                <th>Size</th>
                <th>Files</th>
              </tr>
            </thead>
            <tbody>
              <CategoryRow label="Candidate images" cat={s.cache_candidate_images} clearable />
              <CategoryRow label="Taste offer examples" cat={s.cache_taste_offers} clearable={false} />
              <CategoryRow label="Other" cat={s.cache_other} clearable={false} />
            </tbody>
          </table>
        </div>
        <div className="button-row" style={{ marginTop: '1rem' }}>
          <button onClick={onClearCache} disabled={clearCacheMutation.isPending}>
            {clearCacheMutation.isPending ? 'Clearing...' : 'Clear image cache'}
          </button>
          {clearCacheMutation.isSuccess && (
            <span className="muted">
              Freed {fmtBytes(clearCacheMutation.data.bytes_freed)} ({clearCacheMutation.data.files_removed} files).
            </span>
          )}
        </div>
        <ErrorText error={clearCacheMutation.error} prefix="Clear cache failed" />
        <p className="muted" style={{ marginTop: '1rem' }}>
          Only candidate listing thumbnails are cleared here — they're re-downloaded from Vinted as needed. Taste
          offer examples feed future taste recompute and aren't cleared by this action. Uploads and the database
          are never touched.
        </p>
      </div>
    </section>
  );
}
