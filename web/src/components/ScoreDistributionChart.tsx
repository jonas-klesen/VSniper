import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api } from '../lib/api';
import { queryKeys } from '../lib/queryKeys';
import type { ScoreDistributionBin, ScoreDistributionWindow } from '../types';
import { ErrorText } from './ErrorText';

const WINDOW_OPTIONS: { value: ScoreDistributionWindow; label: string }[] = [
  { value: '1h', label: '1 hour' },
  { value: '6h', label: '6 hours' },
  { value: '12h', label: '12 hours' },
  { value: '1d', label: '24 hours' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: 'all', label: 'All time' },
];

const CHART_WIDTH = 640;
const CHART_HEIGHT = 260;
const MARGIN = { top: 12, right: 12, bottom: 30, left: 34 };
const PLOT_WIDTH = CHART_WIDTH - MARGIN.left - MARGIN.right;
const PLOT_HEIGHT = CHART_HEIGHT - MARGIN.top - MARGIN.bottom;
const MAX_BAR_WIDTH = 24;
const GRID_STEPS = [0, 0.25, 0.5, 0.75, 1];

// Nicely-rounded y-axis ceiling so gridline labels land on clean percentages.
function niceMax(value: number): number {
  if (value <= 0) return 10;
  const step = value <= 20 ? 5 : 10;
  return Math.ceil(value / step) * step;
}

// Column with rounded top corners, square at the baseline.
function topRoundedRectPath(x: number, y: number, width: number, height: number, radius: number): string {
  if (height <= 0) return '';
  const r = Math.min(radius, width / 2, height);
  return [
    `M${x},${y + height}`,
    `L${x},${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `L${x + width - r},${y}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `L${x + width},${y + height}`,
    'Z',
  ].join(' ');
}

export function ScoreDistributionChart() {
  const [selectedWindow, setSelectedWindow] = useState<ScoreDistributionWindow>('7d');
  const [hovered, setHovered] = useState<number | null>(null);

  const query = useQuery({
    queryKey: queryKeys.scoreDistribution(selectedWindow),
    queryFn: () => api.getScoreDistribution(selectedWindow),
    placeholderData: (previous) => previous,
  });

  const data = query.data;
  const bins: ScoreDistributionBin[] = data?.bins ?? [];
  const yMax = niceMax(Math.max(0, ...bins.map((bin) => bin.percentage)));
  const bandWidth = bins.length ? PLOT_WIDTH / bins.length : 0;
  const barWidth = Math.min(MAX_BAR_WIDTH, bandWidth * 0.6);

  return (
    <article className="card">
      <div className="page-header" style={{ marginBottom: '0.5rem' }}>
        <div>
          <h3 style={{ margin: 0 }}>Judge score distribution</h3>
          <span className="muted">
            {data ? `${data.total_count} candidate${data.total_count === 1 ? '' : 's'} judged` : ' '}
          </span>
        </div>
        <div className="button-row">
          {WINDOW_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              className={selectedWindow === opt.value ? '' : 'secondary'}
              onClick={() => setSelectedWindow(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {query.isLoading ? (
        <p className="muted">Loading…</p>
      ) : query.isError ? (
        <ErrorText error={query.error} prefix="Could not load score distribution" />
      ) : !data || data.total_count === 0 ? (
        <p className="muted">No judged candidates in this window yet.</p>
      ) : (
        <div style={{ opacity: query.isFetching ? 0.6 : 1, transition: 'opacity 150ms ease' }}>
          <svg
            viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
            width="100%"
            role="img"
            aria-label="Distribution of judge scores as a percentage of judged candidates"
          >
            <g transform={`translate(${MARGIN.left}, ${MARGIN.top})`}>
              {GRID_STEPS.map((step) => {
                const y = PLOT_HEIGHT - step * PLOT_HEIGHT;
                return (
                  <g key={step}>
                    <line x1={0} x2={PLOT_WIDTH} y1={y} y2={y} stroke="var(--border)" strokeWidth={1} />
                    <text x={-8} y={y} textAnchor="end" dominantBaseline="middle" className="chart-axis-label">
                      {Math.round(step * yMax)}%
                    </text>
                  </g>
                );
              })}

              {bins.map((bin, index) => {
                const barHeight = yMax > 0 ? (bin.percentage / yMax) * PLOT_HEIGHT : 0;
                const bandX = index * bandWidth;
                const barX = bandX + (bandWidth - barWidth) / 2;
                const barY = PLOT_HEIGHT - barHeight;
                const isHovered = hovered === index;
                return (
                  <g
                    key={`${bin.min_score}-${bin.max_score}`}
                    tabIndex={0}
                    role="img"
                    aria-label={`Score ${bin.min_score} to ${bin.max_score}: ${bin.count} candidates, ${bin.percentage}%`}
                    onMouseEnter={() => setHovered(index)}
                    onMouseLeave={() => setHovered(null)}
                    onFocus={() => setHovered(index)}
                    onBlur={() => setHovered(null)}
                    style={{ cursor: 'default' }}
                  >
                    <rect x={bandX} y={0} width={bandWidth} height={PLOT_HEIGHT} fill="transparent" />
                    {barHeight > 0 ? (
                      <path
                        d={topRoundedRectPath(barX, barY, barWidth, barHeight, 4)}
                        fill={isHovered ? 'var(--accent-strong)' : 'var(--accent)'}
                      />
                    ) : null}
                    <text x={bandX + bandWidth / 2} y={PLOT_HEIGHT + 18} textAnchor="middle" className="chart-axis-label">
                      {bin.min_score}-{bin.max_score}
                    </text>
                  </g>
                );
              })}

              <line x1={0} x2={PLOT_WIDTH} y1={PLOT_HEIGHT} y2={PLOT_HEIGHT} stroke="var(--border-strong)" strokeWidth={1} />
            </g>
          </svg>

          <p className="muted" style={{ marginTop: '0.25rem', minHeight: '1.2em' }}>
            {hovered !== null && bins[hovered] ? (
              <>
                <strong>{bins[hovered].min_score}-{bins[hovered].max_score}:</strong>{' '}
                {bins[hovered].count} candidate{bins[hovered].count === 1 ? '' : 's'} ({bins[hovered].percentage}%)
              </>
            ) : (
              ' '
            )}
          </p>
        </div>
      )}
    </article>
  );
}
