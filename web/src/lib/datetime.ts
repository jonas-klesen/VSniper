const BERLIN_DATE_TIME_FORMATTER = new Intl.DateTimeFormat('de-DE', {
  timeZone: 'Europe/Berlin',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
  timeZoneName: 'short',
});

const EXPLICIT_TIME_ZONE_RE = /(?:[zZ]|[+-]\d{2}:?\d{2})$/;

function parseAppDateTime(value: Date | string | null | undefined): Date | null {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;

  const trimmed = value.trim();
  if (!trimmed) return null;

  // SQLite returns Python UTC datetimes without their tzinfo. Treat those app
  // timestamps as UTC instants before displaying them in the Berlin timezone.
  const normalized = EXPLICIT_TIME_ZONE_RE.test(trimmed) ? trimmed : `${trimmed}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatBerlinDateTime(value: Date | string | null | undefined, fallback = ''): string {
  const date = parseAppDateTime(value);
  return date ? BERLIN_DATE_TIME_FORMATTER.format(date) : fallback;
}
