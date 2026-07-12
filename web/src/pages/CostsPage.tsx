import { useQuery } from '@tanstack/react-query';

import { api } from '../lib/api';
import { queryKeys } from '../lib/queryKeys';
import { ErrorText } from '../components/ErrorText';
import type { AiCategoryStats } from '../types';

import { Brain, Scale } from 'lucide-react';

function fmtUsd(usd: number): string {
  return `$${usd.toFixed(4)}`;
}

function CategoryRow({ label, icon, cat }: { label: string; icon: React.ReactNode; cat: AiCategoryStats }) {
  return (
    <tr>
      <td><span style={{ marginRight: '0.4em', verticalAlign: 'middle' }}>{icon}</span>{label}</td>
      <td>{fmtUsd(cat.last_24h_usd)}<span className="muted"> ({cat.last_24h_calls})</span></td>
      <td>{fmtUsd(cat.last_7d_usd)}<span className="muted"> ({cat.last_7d_calls})</span></td>
      <td>{fmtUsd(cat.last_30d_usd)}<span className="muted"> ({cat.last_30d_calls})</span></td>
      <td>{fmtUsd(cat.total_usd)}<span className="muted"> ({cat.total_calls})</span></td>
    </tr>
  );
}

export function CostsPage() {
  const query = useQuery({ queryKey: queryKeys.costs, queryFn: api.getAiCostStats });

  if (query.isLoading) {
    return <p>Loading cost data...</p>;
  }

  if (query.isError || !query.data) {
    return <ErrorText error={query.error ?? 'Could not load cost data.'} />;
  }

  const s = query.data;

  return (
    <section>
      <div className="page-header">
        <div>
          <p className="eyebrow">Usage</p>
          <h2>AI Costs</h2>
        </div>
      </div>

      <div className="card-grid">
        <article className="card stat-card">
          <span>Total spend</span>
          <strong>{fmtUsd(s.total_usd)}</strong>
          <span className="muted">{s.total_calls} call{s.total_calls !== 1 ? 's' : ''}</span>
        </article>
        <article className="card stat-card">
          <span>Last 24 hours</span>
          <strong>{fmtUsd(s.last_24h_usd)}</strong>
          <span className="muted">{s.last_24h_calls} call{s.last_24h_calls !== 1 ? 's' : ''}</span>
        </article>
        <article className="card stat-card">
          <span>Last 7 days</span>
          <strong>{fmtUsd(s.last_7d_usd)}</strong>
          <span className="muted">{s.last_7d_calls} call{s.last_7d_calls !== 1 ? 's' : ''}</span>
        </article>
        <article className="card stat-card">
          <span>Last 30 days</span>
          <strong>{fmtUsd(s.last_30d_usd)}</strong>
          <span className="muted">{s.last_30d_calls} call{s.last_30d_calls !== 1 ? 's' : ''}</span>
        </article>
      </div>

      <div className="card">
        <h3>By operation</h3>
        <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr className="muted">
              <th>Operation</th>
              <th>24 hours</th>
              <th>7 days</th>
              <th>30 days</th>
              <th>All time</th>
            </tr>
          </thead>
          <tbody>
            <CategoryRow label="Judge" icon={<Scale size={14} />} cat={s.judge} />
            <CategoryRow label="Learning" icon={<Brain size={14} />} cat={s.learning} />
          </tbody>
        </table>
        </div>
      </div>

      <div className="card">
        <h3>About cost tracking</h3>
        <p className="muted">
          Costs are calculated from token usage reported by AI provider responses using standard OpenAI tier pricing.
          Prices are estimates - cached tokens, reasoning tokens, and regional uplifts are not broken out separately.
        </p>
      </div>
    </section>
  );
}
