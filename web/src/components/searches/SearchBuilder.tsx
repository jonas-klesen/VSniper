import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { DecimalInput } from '../DecimalInput';
import { ErrorText } from '../ErrorText';
import { useModalDismiss } from '../../lib/useModalDismiss';
import { clothingItemLabel, clothingItems } from '../../lib/clothingItems';
import { formatBerlinDateTime } from '../../lib/datetime';
import {
  applySizeFilter,
  formatFilter,
  isCategoryFilter,
  isGuidedFilter,
  isSizeFilter,
  isPreferKeywordFilter,
  isAvoidKeywordFilter,
  isPriceCeilingFilter,
  priceCeilingFor,
  replaceFilters,
  unique,
  valuesFor,
} from '../../lib/searchFilters';
import type {
  ClothingItem,
  SearchCategoryOption,
  SearchCategoryOptions,
  SearchFilter,
  SearchRecord,
  SearchRunResult,
  SearchUpdatePayload,
} from '../../types';

export type SearchFormState = {
  name: string;
  clothing_item: ClothingItem;
  query: string;
  region: string;
  enabled: boolean;
  filters: SearchFilter[];
  alert_threshold: number | null;
};

const filterModes: SearchFilter['mode'][] = ['include', 'exclude', 'range', 'exact'];

function setCategoryAliases(form: SearchFormState, aliases: string[]): SearchFormState {
  const values = unique(aliases);
  return {
    ...form,
    filters: replaceFilters(
      form.filters,
      isCategoryFilter,
      values.length ? { field: 'category', label: 'Vinted category', mode: 'include', values } : null,
      'front',
    ),
  };
}

type CategoryChoiceGroup = {
  key: string;
  primaryAlias: string;
  aliases: string[];
  catalogIds: string[];
};

function categoryIdsForAlias(option: SearchCategoryOption, alias: string): string[] {
  return option.alias_catalog_ids?.[alias] ?? [alias];
}

function categoryGroupKey(ids: string[]): string {
  return ids.length ? [...ids].sort().join('|') : '__unresolved__';
}

function groupCategoryAliases(option: SearchCategoryOption): CategoryChoiceGroup[] {
  const groups = new Map<string, CategoryChoiceGroup>();
  for (const alias of option.allowed_aliases) {
    const catalogIds = categoryIdsForAlias(option, alias);
    const key = categoryGroupKey(catalogIds);
    const existing = groups.get(key);
    if (existing) {
      existing.aliases.push(alias);
      continue;
    }
    groups.set(key, { key, primaryAlias: alias, aliases: [alias], catalogIds });
  }
  return Array.from(groups.values());
}

function selectedCategoryGroupKeys(option: SearchCategoryOption, selectedAliases: string[]): Set<string> {
  return new Set(selectedAliases.map((alias) => categoryGroupKey(categoryIdsForAlias(option, alias))));
}

function setDefaultCategoryAliases(
  form: SearchFormState,
  categoryOptions?: SearchCategoryOptions,
  clothingItem: ClothingItem = form.clothing_item,
): SearchFormState {
  const defaults = categoryOptions?.[clothingItem]?.default_aliases ?? [];
  return setCategoryAliases({ ...form, clothing_item: clothingItem }, defaults);
}

function setTokenFilter(
  form: SearchFormState,
  predicate: (filter: SearchFilter) => boolean,
  replacement: SearchFilter | null,
): SearchFormState {
  return { ...form, filters: replaceFilters(form.filters, predicate, replacement) };
}

export function normalizePriceCeiling(value: string): string {
  const normalized = value.trim().replace(',', '.');
  if (!normalized) return '';
  const amount = Number(normalized);
  if (!Number.isFinite(amount) || amount <= 0) return '';
  return String(Math.round(amount * 100) / 100);
}

export function setPriceCeiling(form: SearchFormState, value: string): SearchFormState {
  const normalized = normalizePriceCeiling(value);
  return {
    ...form,
    filters: replaceFilters(
      form.filters,
      isPriceCeilingFilter,
      normalized ? { field: 'price', label: 'Maximum price', mode: 'range', values: [normalized] } : null,
    ),
  };
}

export function withDefaultCategoryFilter(
  form: SearchFormState,
  categoryOptions?: SearchCategoryOptions,
): SearchFormState {
  return valuesFor(form.filters, isCategoryFilter).length ? form : setDefaultCategoryAliases(form, categoryOptions);
}

export function formFromSearch(search: SearchRecord): SearchFormState {
  return {
    name: search.name,
    clothing_item: search.clothing_item,
    query: search.query,
    region: 'de',
    enabled: search.enabled,
    filters: search.filters,
    alert_threshold: search.alert_threshold,
  };
}

export function payloadFromForm(form: SearchFormState): Omit<SearchUpdatePayload, 'enabled'> {
  const query = form.query.trim();
  const region = form.region.trim().toLowerCase();
  if (region !== 'de') throw new Error('Only the de Vinted region is supported.');
  return {
    clothing_item: form.clothing_item,
    query,
    region,
    filters: form.filters,
    alert_threshold: form.alert_threshold,
  };
}

function validationIssues(form: SearchFormState): string[] {
  const issues: string[] = [];
  if (!form.query.trim()) issues.push('Add a Vinted query, for example “cargo patchwork” or “vintage adidas”.');
  return issues;
}

function ChipEditor({
  values,
  placeholder,
  onChange,
}: {
  values: string[];
  placeholder: string;
  onChange: (values: string[]) => void;
}) {
  const [draft, setDraft] = useState('');

  const addValues = (raw: string) => {
    const next = unique([...values, ...raw.split(',').map((value) => value.trim())]);
    onChange(next);
    setDraft('');
  };

  const removeValue = (value: string) => onChange(values.filter((item) => item !== value));

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      addValues(draft);
    }
    if (event.key === 'Backspace' && !draft && values.length) {
      removeValue(values[values.length - 1]);
    }
  };

  return (
    <div className="token-input">
      {values.map((value) => (
        <button type="button" className="token-chip" key={value} onClick={() => removeValue(value)} title={`Remove ${value}`}>
          {value} <span aria-hidden="true">×</span>
        </button>
      ))}
      <input
        value={draft}
        placeholder={placeholder}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => { if (draft.trim()) addValues(draft); }}
        onKeyDown={handleKeyDown}
      />
    </div>
  );
}

function TokenInput({
  label,
  description,
  values,
  placeholder,
  onChange,
}: {
  label: string;
  description: string;
  values: string[];
  placeholder: string;
  onChange: (values: string[]) => void;
}) {
  return (
    <div className="search-builder-field">
      <div className="search-builder-field__header">
        <strong>{label}</strong>
        <span>{description}</span>
      </div>
      <ChipEditor values={values} placeholder={placeholder} onChange={onChange} />
    </div>
  );
}

function CategoryPicker({
  form,
  categoryOptions,
  onChange,
}: {
  form: SearchFormState;
  categoryOptions?: SearchCategoryOptions;
  onChange: (form: SearchFormState) => void;
}) {
  const option = categoryOptions?.[form.clothing_item];
  const selected = valuesFor(form.filters, isCategoryFilter);
  const visibleSelection = selected.length ? selected : option?.default_aliases ?? [];
  const groupedChoices = option ? groupCategoryAliases(option) : [];
  const selectedGroupKeys = option ? selectedCategoryGroupKeys(option, visibleSelection) : new Set<string>();

  const toggleGroup = (group: CategoryChoiceGroup) => {
    const aliasesInGroup = new Set(group.aliases);
    const active = selectedGroupKeys.has(group.key);
    const next = active
      ? visibleSelection.filter((value) => !aliasesInGroup.has(value))
      : [...visibleSelection.filter((value) => !aliasesInGroup.has(value)), group.primaryAlias];
    onChange(setCategoryAliases(form, next));
  };

  return (
    <div className="search-builder-section">
      <div className="search-builder-section__header">
        <div>
          <h4>Vinted category</h4>
          <p className="muted">Choose safe category aliases for {clothingItemLabel(form.clothing_item)}. This keeps Vinted from searching the wrong closet aisle.</p>
        </div>
        <button
          type="button"
          className="secondary inline-action"
          onClick={() => onChange(setDefaultCategoryAliases(form, categoryOptions))}
          disabled={!option}
        >
          Use defaults
        </button>
      </div>
      {!option ? (
        <p className="muted">Loading category choices…</p>
      ) : (
        <>
          <div className="choice-chip-row">
            {groupedChoices.map((group) => (
              <button
                type="button"
                className={`choice-chip ${selectedGroupKeys.has(group.key) ? 'active' : ''}`}
                key={group.key}
                onClick={() => toggleGroup(group)}
                title={group.aliases.length > 1 ? `Vinted catalog ID${group.catalogIds.length > 1 ? 's' : ''} ${group.catalogIds.join(', ')}: ${group.aliases.join(', ')}` : undefined}
              >
                {group.primaryAlias}{group.aliases.length > 1 ? ` (+${group.aliases.length - 1})` : ''}
              </button>
            ))}
          </div>
          <p className="muted small-copy">
            Defaults: {option.default_aliases.join(', ')} · resolves to {option.resolved_catalog_ids.length} Vinted catalog IDs.
          </p>
        </>
      )}
    </div>
  );
}

function GuidedFilterEditor({
  form,
  categoryOptions,
  onChange,
  onSyncSizes,
  isSyncingSizes,
}: {
  form: SearchFormState;
  categoryOptions?: SearchCategoryOptions;
  onChange: (form: SearchFormState) => void;
  onSyncSizes: () => void;
  isSyncingSizes: boolean;
}) {
  const sizes = valuesFor(form.filters, isSizeFilter);
  const prefer = valuesFor(form.filters, isPreferKeywordFilter);
  const avoid = valuesFor(form.filters, isAvoidKeywordFilter);
  const priceCeiling = priceCeilingFor(form.filters);

  return (
    <div className="search-builder-rules">
      <CategoryPicker form={form} categoryOptions={categoryOptions} onChange={onChange} />

      <div className="search-builder-side-stack">
        <div className="search-builder-section">
          <div className="search-builder-section__header">
            <div>
              <h4>Sizes</h4>
              <p className="muted">Sync from your Vinted profile, or add and remove size chips directly here.</p>
            </div>
            <button type="button" className="secondary" onClick={onSyncSizes} disabled={isSyncingSizes}>
              {isSyncingSizes ? 'Syncing…' : '↻ Sync from profile'}
            </button>
          </div>
          <ChipEditor
            values={sizes}
            placeholder="M, L, 42…"
            onChange={(values) => onChange({ ...form, filters: applySizeFilter(form.filters, values) })}
          />
        </div>

        <div className="search-builder-section">
          <div className="search-builder-section__header">
            <div>
              <h4>Maximum price</h4>
              <p className="muted">Manual upper limit in euro. Saved as Vinted <code>price_to</code> with EUR currency.</p>
            </div>
            {priceCeiling ? <button type="button" className="secondary inline-action" onClick={() => onChange(setPriceCeiling(form, ''))}>Clear</button> : null}
          </div>
          <label className="price-ceiling-field">
            <span>Up to</span>
            <DecimalInput
              type="text"
              inputMode="decimal"
              placeholder="50"
              value={priceCeiling}
              normalize={normalizePriceCeiling}
              onCommit={(normalized) => onChange(setPriceCeiling(form, normalized))}
            />
            <span>€</span>
          </label>
        </div>
      </div>

      <div className="search-builder-section two-column builder-keyword-grid">
        <TokenInput
          label="Prefer keywords"
          description="Terms that should raise confidence before the visual judge runs."
          placeholder="cargo, patchwork, Y2K…"
          values={prefer}
          onChange={(values) => onChange(setTokenFilter(
            form,
            isPreferKeywordFilter,
            values.length ? { field: 'keywords', label: 'Prefer', mode: 'include', values } : null,
          ))}
        />
        <TokenInput
          label="Avoid keywords"
          description="Terms that should push obvious misses away early."
          placeholder="office, plain, logo…"
          values={avoid}
          onChange={(values) => onChange(setTokenFilter(
            form,
            isAvoidKeywordFilter,
            values.length ? { field: 'keywords', label: 'Avoid', mode: 'exclude', values } : null,
          ))}
        />
      </div>
    </div>
  );
}

function FilterValuesInput({ values, onCommit }: { values: string[]; onCommit: (values: string[]) => void }) {
  // Keep the raw text locally so typing a comma (or trailing spaces) isn't erased
  // mid-keystroke; only parse into discrete values on blur.
  const [raw, setRaw] = useState(values.join(', '));

  // Resync when the committed values change from outside (e.g. a different filter row).
  useEffect(() => {
    setRaw(values.join(', '));
  }, [values]);

  const commit = () => {
    const parsed = unique(raw.split(','));
    onCommit(parsed);
    setRaw(parsed.join(', '));
  };

  return (
    <input
      placeholder="value1, value2"
      value={raw}
      onChange={(event) => setRaw(event.target.value)}
      onBlur={commit}
    />
  );
}

function AdvancedFilterEditor({ filters, onChange }: { filters: SearchFilter[]; onChange: (filters: SearchFilter[]) => void }) {
  const update = (index: number, patch: Partial<SearchFilter>) =>
    onChange(filters.map((filter, i) => (i === index ? { ...filter, ...patch } : filter)));
  const remove = (index: number) => onChange(filters.filter((_, i) => i !== index));
  const add = () => onChange([...filters, { field: '', label: '', mode: 'include', values: [] }]);

  return (
    <details className="advanced-filters">
      <summary>Advanced filters</summary>
      <p className="muted small-copy">For custom Vinted fields or legacy filters. Common category, size, prefer, and avoid rules are managed above.</p>
      <div className="filter-editor">
        {filters.length > 0 && (
          <div className="filter-row filter-row-header">
            <span>Field</span>
            <span>Label</span>
            <span>Mode</span>
            <span>Values</span>
            <span />
          </div>
        )}
        {filters.map((filter, index) => (
          <div className="filter-row" key={index}>
            <input
              placeholder="e.g. brand"
              value={filter.field}
              onChange={(event) => update(index, { field: event.target.value })}
            />
            <input
              placeholder="e.g. Brand"
              value={filter.label}
              onChange={(event) => update(index, { label: event.target.value })}
            />
            <select
              value={filter.mode}
              onChange={(event) => update(index, { mode: event.target.value as SearchFilter['mode'] })}
            >
              {filterModes.map((mode) => <option key={mode} value={mode}>{mode}</option>)}
            </select>
            <FilterValuesInput
              values={filter.values}
              onCommit={(values) => update(index, { values })}
            />
            <button type="button" className="secondary inline-action remove-filter" onClick={() => remove(index)} title="Remove filter">×</button>
          </div>
        ))}
        <button type="button" className="secondary inline-action" onClick={add}>+ Add advanced filter</button>
      </div>
    </details>
  );
}

export function SearchBuilderModal({
  form,
  categoryOptions,
  onChange,
  onClose,
  onSubmit,
  isPending,
  error,
  onSyncSizes,
  isSyncingSizes,
  syncSizesError,
}: {
  form: SearchFormState;
  categoryOptions?: SearchCategoryOptions;
  onChange: (form: SearchFormState) => void;
  onClose: () => void;
  onSubmit: () => void;
  isPending: boolean;
  error: string | null;
  onSyncSizes: () => void;
  isSyncingSizes: boolean;
  syncSizesError?: unknown;
}) {
  const issues = validationIssues(form);
  const advancedFilters = useMemo(() => form.filters.filter((filter) => !isGuidedFilter(filter)), [form.filters]);
  const selectedItem = clothingItems.find((item) => item.value === form.clothing_item);
  const searchLabel = clothingItemLabel(form.clothing_item);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalDismiss(dialogRef, onClose, isPending);

  const setAdvancedFilters = (nextAdvanced: SearchFilter[]) => {
    onChange({ ...form, filters: [...form.filters.filter(isGuidedFilter), ...nextAdvanced] });
  };

  return createPortal(
    <div className="modal-overlay" onClick={() => { if (!isPending) onClose(); }}>
      <div
        className="modal search-builder-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Edit category search — ${searchLabel}`}
        ref={dialogRef}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header search-builder-header">
          <div>
            <p className="eyebrow">Edit category search</p>
            <h3>{searchLabel}</h3>
          </div>
          <div className="search-builder-header__actions">
            <span className={`pill ${form.enabled ? 'healthy' : 'missing'}`}>{form.enabled ? 'enabled' : 'disabled'}</span>
            <button className="secondary modal-close" onClick={onClose} disabled={isPending}>×</button>
          </div>
        </div>

        <div className="search-builder-body">
          <section className="search-builder-section">
            <div className="search-builder-section__header">
              <div>
                <h4>Basics</h4>
                <p className="muted">Each clothing bucket has exactly one search. Tune the query and filters here.</p>
              </div>
              <span className="region-note">Region: Germany / de only</span>
            </div>
            <div className="form-grid three-up">
              <div className="readonly-field">
                <span>Search</span>
                <strong>{searchLabel}</strong>
                <small>Fixed bucket name</small>
              </div>
              <label>
                Vinted query
                <input value={form.query} onChange={(event) => onChange({ ...form, query: event.target.value })} />
              </label>
              <div className="readonly-field">
                <span>Clothing item</span>
                <strong>{clothingItemLabel(form.clothing_item)}</strong>
                <small>Fixed bucket</small>
              </div>
            </div>
            {selectedItem ? <p className="muted small-copy">{selectedItem.description}</p> : null}
            <label className="checkbox-row builder-enabled-row">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(event) => onChange({ ...form, enabled: event.target.checked })}
              />
              Enabled — worker scans this search automatically.
            </label>
            {issues.length ? (
              <div className="builder-validation">
                {issues.map((issue) => <p key={issue}>{issue}</p>)}
              </div>
            ) : null}
          </section>

          <GuidedFilterEditor
            form={form}
            categoryOptions={categoryOptions}
            onChange={onChange}
            onSyncSizes={onSyncSizes}
            isSyncingSizes={isSyncingSizes}
          />
          <ErrorText error={syncSizesError} prefix="Size sync failed" />

          <AdvancedFilterEditor filters={advancedFilters} onChange={setAdvancedFilters} />
        </div>

        <div className="button-row form-actions search-builder-footer">
          <button onClick={onSubmit} disabled={isPending || issues.length > 0}>{isPending ? 'Saving…' : 'Save changes'}</button>
          <button className="secondary" onClick={onClose} disabled={isPending}>Cancel</button>
        </div>
        <ErrorText error={error} />
      </div>
    </div>,
    document.body,
  );
}

export function SearchCard({
  search,
  globalAlertThreshold,
  onEdit,
  onToggle,
  onLiveRun,
  onQuickSave,
  runResult,
  errors,
  isBusy,
}: {
  search: SearchRecord;
  globalAlertThreshold: number;
  onEdit: () => void;
  onToggle: () => void;
  onLiveRun: () => void;
  onQuickSave: (payload: SearchUpdatePayload) => void;
  runResult?: SearchRunResult;
  errors: {
    toggle?: unknown;
    live?: unknown;
    quick?: unknown;
  };
  isBusy: {
    live: boolean;
    quick: boolean;
  };
}) {
  const categoryValues = valuesFor(search.filters, isCategoryFilter);
  const sizeValues = valuesFor(search.filters, isSizeFilter);
  const preferValues = valuesFor(search.filters, isPreferKeywordFilter);
  const avoidValues = valuesFor(search.filters, isAvoidKeywordFilter);
  const priceCeiling = priceCeilingFor(search.filters);
  const advancedCount = search.filters.filter((filter) => !isGuidedFilter(filter)).length;
  const canRun = search.query.trim().length > 0;
  const saveForm = (form: SearchFormState) => {
    const payload = payloadFromForm(form);
    onQuickSave({ ...payload, enabled: form.enabled });
  };
  const savePriceCeiling = (value: string) => {
    const next = setPriceCeiling(formFromSearch(search), value);
    saveForm(next);
  };
  const saveAlertThreshold = (value: string) => {
    const parsed = value === 'default' ? null : Number(value);
    saveForm({ ...formFromSearch(search), alert_threshold: parsed });
  };

  return (
    <article className="card search-card">
      <div className="row-between search-card__topline">
        <div>
          <h3>{search.name}</h3>
          <p className="muted search-card__summary">
            {clothingItemLabel(search.clothing_item)} · {search.query} · region {search.region}
          </p>
        </div>
        <span className={`pill ${search.enabled ? 'healthy' : 'missing'}`}>{search.enabled ? 'enabled' : 'disabled'}</span>
      </div>

      <div className="search-card__rules">
        {categoryValues.length ? <span className="tag">Category: {categoryValues.join(', ')}</span> : null}
        {sizeValues.length ? <span className="tag">Sizes: {sizeValues.join(', ')}</span> : null}
        {priceCeiling ? <span className="tag">Max price: {priceCeiling} €</span> : null}
        {preferValues.length ? <span className="tag">Prefer: {preferValues.join(', ')}</span> : null}
        {avoidValues.length ? <span className="tag concern">Avoid: {avoidValues.join(', ')}</span> : null}
        {advancedCount ? <span className="tag subtle">+ {advancedCount} advanced</span> : null}
        {!search.filters.length ? <span className="muted">No saved filters; category defaults will be applied by the backend.</span> : null}
      </div>

      <div className="search-card__quick-edit">
        <label>
          Max price
          <span className="quick-edit-input">
            <DecimalInput
              type="text"
              inputMode="decimal"
              placeholder="none"
              value={priceCeiling}
              normalize={normalizePriceCeiling}
              onCommit={savePriceCeiling}
              disabled={isBusy.quick}
            />
            <span>€</span>
          </span>
        </label>
        <label>
          Alert threshold
          <select
            value={search.alert_threshold === null ? 'default' : String(search.alert_threshold)}
            onChange={(event) => saveAlertThreshold(event.target.value)}
            disabled={isBusy.quick}
          >
            <option value="default">Default ({globalAlertThreshold})</option>
            {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
          <span className="field-help">Current alert: {search.effective_alert_threshold}+</span>
        </label>
      </div>

      <div className="search-card__meta">
        <span>Last run: {formatBerlinDateTime(search.last_run_at, 'never')}</span>
        <span>Fetched: {search.last_fetched_count}</span>
        <span>Judged: {search.last_judged_count}</span>
        <span>Alerts: {search.last_found_count}</span>
      </div>

      {search.filters.length ? (
        <details className="search-card__details">
          <summary>All filters</summary>
          <p className="muted small-copy" style={{ margin: '0.55rem 0 0' }}>
            {search.filters.map(formatFilter).join(' · ')}
          </p>
        </details>
      ) : null}

      <div className="button-row compact-actions search-card__actions">
        <button onClick={onEdit}>Edit</button>
        <button className="secondary" onClick={onToggle}>{search.enabled ? 'Disable' : 'Enable'}</button>
        <button className="secondary" onClick={onLiveRun} disabled={isBusy.live || !canRun} title={canRun ? undefined : 'Add a Vinted query before running this search.'}>Live run</button>
      </div>
      {!canRun ? <p className="muted small-copy">Add a Vinted query before running this category search.</p> : null}

      {runResult ? <p className="run-result">{runResult.summary}</p> : null}
      <ErrorText error={errors.toggle} prefix="Toggle failed" />
      <ErrorText error={errors.live} prefix="Live run failed" />
      <ErrorText error={errors.quick} prefix="Quick edit failed" />
    </article>
  );
}
