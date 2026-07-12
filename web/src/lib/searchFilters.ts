import type { SearchFilter } from '../types';

const categoryFields = new Set(['category', 'catalog']);
const keywordFields = new Set(['keyword', 'keywords']);

export function normaliseFilterText(value: string): string {
  return value.trim().toLowerCase().replace(/_/g, ' ');
}

export function unique(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

export function isCategoryFilter(filter: SearchFilter): boolean {
  return categoryFields.has(normaliseFilterText(filter.field));
}

export function isSizeFilter(filter: SearchFilter): boolean {
  return normaliseFilterText(filter.field) === 'size';
}

export function isKeywordFilter(filter: SearchFilter): boolean {
  return keywordFields.has(normaliseFilterText(filter.field));
}

export function isPreferKeywordFilter(filter: SearchFilter): boolean {
  return isKeywordFilter(filter) && filter.mode === 'include';
}

export function isAvoidKeywordFilter(filter: SearchFilter): boolean {
  return isKeywordFilter(filter) && filter.mode === 'exclude';
}

export function isPriceCeilingFilter(filter: SearchFilter): boolean {
  return normaliseFilterText(filter.field) === 'price' && filter.mode === 'range';
}

export function isGuidedFilter(filter: SearchFilter): boolean {
  return isCategoryFilter(filter)
    || isSizeFilter(filter)
    || isPreferKeywordFilter(filter)
    || isAvoidKeywordFilter(filter)
    || isPriceCeilingFilter(filter);
}

export function valuesFor(filters: SearchFilter[], predicate: (filter: SearchFilter) => boolean): string[] {
  return unique(filters.filter(predicate).flatMap((filter) => filter.values));
}

export function priceCeilingFor(filters: SearchFilter[]): string {
  const values = filters
    .filter(isPriceCeilingFilter)
    .flatMap((filter) => filter.values)
    .map((value) => value.trim())
    .filter(Boolean);
  return values.length ? values[values.length - 1] : '';
}

export function replaceFilters(
  filters: SearchFilter[],
  predicate: (filter: SearchFilter) => boolean,
  replacement: SearchFilter | null,
  placement: 'front' | 'back' = 'back',
): SearchFilter[] {
  const kept = filters.filter((filter) => !predicate(filter));
  if (!replacement || replacement.values.length === 0) return kept;
  return placement === 'front' ? [replacement, ...kept] : [...kept, replacement];
}

export function applySizeFilter(filters: SearchFilter[], sizes: string[]): SearchFilter[] {
  const values = unique(sizes);
  return replaceFilters(
    filters,
    isSizeFilter,
    values.length ? { field: 'size', label: 'Sizes', mode: 'include', values } : null,
    'front',
  );
}

export function formatFilter(filter: SearchFilter): string {
  const label = filter.label || filter.field || 'Filter';
  const values = filter.values.length ? filter.values.join(', ') : '—';
  return `${label}: ${values}`;
}

export function formatSearchFilters(filters: SearchFilter[]): string {
  if (!filters.length) return 'No filters';
  return filters.map(formatFilter).join(' · ');
}

export function filterGroupKey(filter: SearchFilter): string {
  if (isCategoryFilter(filter)) return 'category';
  if (isSizeFilter(filter)) return 'size';
  if (isPreferKeywordFilter(filter)) return 'keywords:include';
  if (isAvoidKeywordFilter(filter)) return 'keywords:exclude';
  if (isPriceCeilingFilter(filter)) return 'price:range';
  return [
    'advanced',
    normaliseFilterText(filter.field),
    filter.mode,
    normaliseFilterText(filter.label),
  ].join(':');
}

export function filterGroupLabel(key: string, filters: SearchFilter[] = []): string {
  if (key === 'category') return 'Vinted category';
  if (key === 'size') return 'Sizes';
  if (key === 'keywords:include') return 'Prefer keywords';
  if (key === 'keywords:exclude') return 'Avoid keywords';
  if (key === 'price:range') return 'Maximum price';
  const first = filters[0];
  return first?.label || first?.field || 'Advanced filter';
}

export function groupFilters(filters: SearchFilter[]): Map<string, SearchFilter[]> {
  const grouped = new Map<string, SearchFilter[]>();
  for (const filter of filters) {
    const key = filterGroupKey(filter);
    grouped.set(key, [...(grouped.get(key) ?? []), filter]);
  }
  return grouped;
}

export function replaceFilterGroup(filters: SearchFilter[], key: string, replacements: SearchFilter[]): SearchFilter[] {
  const kept = filters.filter((filter) => filterGroupKey(filter) !== key);
  if (!replacements.length) return kept;
  if (key === 'category' || key === 'size') return [...replacements, ...kept];
  return [...kept, ...replacements];
}
