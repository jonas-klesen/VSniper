import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api, ApiError } from '../lib/api';
import { clothingItemLabel, clothingItems } from '../lib/clothingItems';
import { formatBerlinDateTime } from '../lib/datetime';
import { queryKeys } from '../lib/queryKeys';
import { useModalDismiss } from '../lib/useModalDismiss';
import { useUnsavedGuard } from '../lib/useUnsavedGuard';
import {
  filterGroupLabel,
  formatSearchFilters,
  groupFilters,
  isPriceCeilingFilter,
  replaceFilterGroup,
} from '../lib/searchFilters';
import { ErrorText } from '../components/ErrorText';
import type {
  ClothingItem,
  ClothingItemTasteProfile,
  GeneratedSearchDraft,
  LearningSnapshot,
  ReferenceObservation,
  SearchFilter,
  SearchRecord,
  SearchUpdatePayload,
  TasteSample,
  WardrobeZipImportResult,
} from '../types';

function fmtUsd(value: number): string {
  return `$${value.toFixed(4)}`;
}

// What the VLM read from a sample's photo. Surfaced so a mis-read (e.g. wrong garment type)
// is visible when debugging why the judge keeps missing the user's taste.
const OBSERVATION_FIELDS: { key: keyof ReferenceObservation; label: string }[] = [
  { key: 'garment_type', label: 'Garment' },
  { key: 'silhouette_and_cut', label: 'Silhouette & cut' },
  { key: 'color_palette', label: 'Colour palette' },
  { key: 'fabric_and_texture', label: 'Fabric & texture' },
  { key: 'prints_or_patterns', label: 'Prints / patterns' },
  { key: 'details_and_hardware', label: 'Details & hardware' },
  { key: 'era_or_subculture', label: 'Era / subculture' },
];

function ObservationDetails({ observation }: { observation: ReferenceObservation }) {
  const rows = OBSERVATION_FIELDS
    .map((field) => [field.label, String(observation[field.key] ?? '').trim()] as const)
    .filter(([, value]) => value);
  if (!rows.length && !observation.vibe_keywords?.length) return null;
  return (
    <dl className="rubric-list" style={{ marginTop: '1rem' }}>
      {rows.map(([label, value]) => (
        <div className="rubric-list__row" key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
      {observation.vibe_keywords?.length ? (
        <div className="rubric-list__row">
          <dt>Vibe</dt>
          <dd>
            <div className="tag-row" style={{ margin: 0 }}>
              {observation.vibe_keywords.map((kw) => <span className="tag subtle" key={kw}>{kw}</span>)}
            </div>
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

// ─── Upload modal ─────────────────────────────────────────────────────────────

function UploadModal({
  onClose,
  onUploadFile,
  clothingItem,
}: {
  onClose: () => void;
  onUploadFile: (file: File, note: string) => Promise<unknown>;
  clothingItem: ClothingItem;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [note, setNote] = useState('');
  const [dragging, setDragging] = useState(false);
  const [previews, setPreviews] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalDismiss(dialogRef, onClose, submitting);

  // Build preview object URLs from the current files and revoke them whenever the
  // file list changes or the modal unmounts — otherwise every re-render (e.g. a
  // keystroke in the note field) would leak a fresh blob URL.
  useEffect(() => {
    const urls = files.map((f) => URL.createObjectURL(f));
    setPreviews(urls);
    return () => urls.forEach((url) => URL.revokeObjectURL(url));
  }, [files]);

  const absorb = (fileList: FileList | null) => {
    if (!fileList) return;
    const incoming = Array.from(fileList);
    const accepted = incoming.filter((f) => f.type.startsWith('image/'));
    const rejected = incoming.length - accepted.length;
    setNotice(rejected > 0 ? `${rejected} non-image file${rejected > 1 ? 's were' : ' was'} skipped.` : null);
    // Append to the existing selection, de-duping by name + size so re-picking the same file is a no-op.
    setFiles((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}:${f.size}`));
      return [...prev, ...accepted.filter((f) => !seen.has(`${f.name}:${f.size}`))];
    });
  };

  const submit = async () => {
    if (!files.length || submitting) return;
    setSubmitting(true);
    setError(null);
    const remaining = [...files];
    try {
      while (remaining.length) {
        await onUploadFile(remaining[0], note);
        remaining.shift();
        setFiles([...remaining]); // drop succeeded files so a retry can't duplicate them
      }
      onClose();
    } catch (err) {
      setFiles([...remaining]); // keep the failed file plus any not yet attempted
      setError(err);
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="modal-overlay" onClick={() => { if (!submitting) onClose(); }}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Add wardrobe photos"
        ref={dialogRef}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3>Add wardrobe photos · {clothingItemLabel(clothingItem)}</h3>
          <button className="modal-close secondary" onClick={onClose} disabled={submitting}>×</button>
        </div>

        <div
          className={`drop-zone${dragging ? ' dragging' : ''}${files.length ? ' has-files' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); absorb(e.dataTransfer.files); }}
          onClick={() => inputRef.current?.click()}
        >
          <input
            ref={inputRef}
            type="file"
            accept="image/*"
            multiple
            style={{ display: 'none' }}
            onChange={(e) => absorb(e.target.files)}
          />
          {files.length > 0 ? (
            <div className="drop-zone-preview">
              {files.map((f, i) => (
                <div key={i} className="drop-zone-thumb">
                  {previews[i] && <img src={previews[i]} alt="" />}
                </div>
              ))}
            </div>
          ) : (
            <div className="drop-zone-empty">
              <span className="drop-zone-plus">+</span>
              <p>Drop photos here or click to browse</p>
              <p className="muted" style={{ fontSize: '0.82rem', marginTop: '0.25rem' }}>Multiple images supported</p>
            </div>
          )}
        </div>

        {files.length > 0 && (
          <p style={{ fontSize: '0.85rem', marginTop: '0.6rem', color: '#9db0db' }}>
            {files.length} photo{files.length > 1 ? 's' : ''} selected
            <button
              className="secondary"
              style={{ marginLeft: '0.75rem', padding: '0.2rem 0.6rem', fontSize: '0.78rem' }}
              onClick={(e) => { e.stopPropagation(); setFiles([]); setNotice(null); }}
              disabled={submitting}
            >
              Clear
            </button>
          </p>
        )}

        <label style={{ marginTop: '1rem', display: 'block' }}>
          Note <span className="muted" style={{ fontSize: '0.82rem' }}>(optional)</span>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. vintage style, favourite piece…"
          />
        </label>

        <div className="button-row" style={{ marginTop: '1.25rem' }}>
          <button className="secondary" onClick={onClose} disabled={submitting}>Cancel</button>
          <button onClick={submit} disabled={!files.length || submitting}>
            {submitting ? 'Uploading…' : `Upload ${files.length > 1 ? `${files.length} photos` : 'photo'}`}
          </button>
        </div>
        {notice ? <p className="muted" style={{ fontSize: '0.82rem', marginTop: '0.6rem' }}>{notice}</p> : null}
        <ErrorText error={error} prefix="Upload failed" />
      </div>
    </div>,
    document.body,
  );
}

// ─── Add offer by URL modal ─────────────────────────────────────────────────────

function AddOfferModal({
  onClose,
  onSubmit,
}: {
  onClose: () => void;
  onSubmit: (payload: {
    vinted_url: string;
    kind: 'offer_like' | 'offer_dislike';
    clothing_item: ClothingItem;
    note: string;
  }) => Promise<unknown>;
}) {
  const [url, setUrl] = useState('');
  const [kind, setKind] = useState<'offer_like' | 'offer_dislike'>('offer_like');
  const [clothingItem, setClothingItem] = useState<ClothingItem>('hosen');
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalDismiss(dialogRef, onClose, submitting);

  const submit = async () => {
    if (!url.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({ vinted_url: url.trim(), kind, clothing_item: clothingItem, note: note.trim() });
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div className="modal-overlay" onClick={() => { if (!submitting) onClose(); }}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Add offer by URL"
        ref={dialogRef}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3>Add offer by URL</h3>
          <button className="modal-close secondary" onClick={onClose} disabled={submitting}>×</button>
        </div>

        <label style={{ display: 'block' }}>
          Vinted listing URL
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="vinted.de/items/… or https://www.vinted.de/items/…"
            autoFocus
          />
        </label>

        <label style={{ marginTop: '1rem', display: 'block' }}>
          Verdict
          <select value={kind} onChange={(e) => setKind(e.target.value as 'offer_like' | 'offer_dislike')}>
            <option value="offer_like">Liked</option>
            <option value="offer_dislike">Disliked</option>
          </select>
        </label>

        <label style={{ marginTop: '1rem', display: 'block' }}>
          Clothing item
          <select value={clothingItem} onChange={(e) => setClothingItem(e.target.value as ClothingItem)}>
            {clothingItems.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
        </label>

        <label style={{ marginTop: '1rem', display: 'block' }}>
          Note <span className="muted" style={{ fontSize: '0.82rem' }}>(optional)</span>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Why does this match (or not)?"
          />
        </label>

        <div className="button-row" style={{ marginTop: '1.25rem' }}>
          <button className="secondary" onClick={onClose} disabled={submitting}>Cancel</button>
          <button onClick={submit} disabled={!url.trim() || submitting}>
            {submitting ? 'Adding…' : 'Add offer'}
          </button>
        </div>
        <ErrorText error={error} prefix="Add offer failed" />
      </div>
    </div>,
    document.body,
  );
}

// ─── Sample detail modal ───────────────────────────────────────────────────────

function SampleDetailModal({
  sample,
  onClose,
  onUpdate,
  onDelete,
  isUpdating,
  error,
}: {
  sample: TasteSample;
  onClose: () => void;
  onUpdate: (note: string, clothingItem: ClothingItem, kind?: 'offer_like' | 'offer_dislike') => void;
  onDelete: () => void;
  isUpdating: boolean;
  error: unknown;
}) {
  const [note, setNote] = useState(sample.note);
  const [clothingItem, setClothingItem] = useState<ClothingItem>(sample.clothing_item);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const isOffer = sample.kind === 'offer_like' || sample.kind === 'offer_dislike';
  const [offerKind, setOfferKind] = useState<'offer_like' | 'offer_dislike'>(
    sample.kind === 'offer_like' || sample.kind === 'offer_dislike' ? sample.kind : 'offer_like',
  );
  const image = sample.image_urls[0];
  // The first analysed image's observation is the one matching the displayed photo.
  const observation = sample.image_observations?.[0]?.observation ?? null;
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalDismiss(dialogRef, onClose, isUpdating);

  return createPortal(
    <div className="modal-overlay" onClick={() => { if (!isUpdating) onClose(); }}>
      <div
        className="modal modal--detail"
        role="dialog"
        aria-modal="true"
        aria-label={sample.title || 'Item'}
        ref={dialogRef}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3 style={{ fontSize: '1rem' }}>{sample.title || 'Item'}</h3>
          <button className="modal-close secondary" onClick={onClose} disabled={isUpdating}>×</button>
        </div>
        <div className="sample-detail-body">
          {image && <img className="sample-detail-image" src={image} alt="" />}
          <div className="sample-detail-info">
            {(sample.brand || sample.size || sample.price_eur) && (
              <p className="muted" style={{ fontSize: '0.88rem', margin: '0 0 0.75rem' }}>
                {[sample.brand, sample.size ? `size ${sample.size}` : '', sample.price_eur != null ? `€${sample.price_eur.toFixed(2)}` : '']
                  .filter(Boolean).join(' · ')}
              </p>
            )}
            <p className="muted" style={{ fontSize: '0.88rem', margin: '0 0 0.75rem' }}>
              {clothingItemLabel(sample.clothing_item)}
            </p>
            {sample.vinted_url && (
              <a href={sample.vinted_url} target="_blank" rel="noreferrer" style={{ fontSize: '0.88rem' }}>
                Open on Vinted ↗
              </a>
            )}
            {isOffer && (
              <label style={{ marginTop: '1rem', display: 'block' }}>
                Verdict
                <select value={offerKind} onChange={(e) => setOfferKind(e.target.value as 'offer_like' | 'offer_dislike')}>
                  <option value="offer_like">Liked</option>
                  <option value="offer_dislike">Disliked</option>
                </select>
              </label>
            )}
            <label style={{ marginTop: '1rem', display: 'block' }}>
              Clothing item
              <select value={clothingItem} onChange={(e) => setClothingItem(e.target.value as ClothingItem)}>
                {clothingItems.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </label>
            <label style={{ marginTop: '1rem', display: 'block' }}>
              Note
              <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} />
            </label>
            {observation ? (
              <div style={{ marginTop: '1rem' }}>
                <p className="eyebrow" style={{ margin: 0 }}>AI read this photo as</p>
                <ObservationDetails observation={observation} />
              </div>
            ) : (
              <p className="muted" style={{ fontSize: '0.82rem', marginTop: '1rem' }}>
                No AI observation yet — recompute your taste profile to analyse this photo.
              </p>
            )}
          </div>
        </div>
        <div className="button-row" style={{ marginTop: '1rem' }}>
          {confirmingDelete ? (
            <>
              <span className="muted" style={{ fontSize: '0.88rem', alignSelf: 'center' }}>Remove this sample?</span>
              <button className="secondary" style={{ color: '#ff9f9f' }} onClick={onDelete} disabled={isUpdating}>Yes, remove</button>
              <button className="secondary" onClick={() => setConfirmingDelete(false)} disabled={isUpdating}>Cancel</button>
            </>
          ) : (
            <>
              <button className="secondary" style={{ color: '#ff9f9f' }} onClick={() => setConfirmingDelete(true)} disabled={isUpdating}>
                {isUpdating ? 'Working…' : 'Remove'}
              </button>
              <button onClick={() => onUpdate(note, clothingItem, isOffer ? offerKind : undefined)} disabled={isUpdating}>
                {isUpdating ? 'Saving…' : 'Save'}
              </button>
            </>
          )}
        </div>
        <ErrorText error={error} prefix="Sample update failed" />
      </div>
    </div>,
    document.body,
  );
}

// ─── Image grid ───────────────────────────────────────────────────────────────

function ImageGrid({
  samples,
  onAdd,
  onDelete,
  onUpdate,
}: {
  samples: TasteSample[];
  onAdd?: () => void;
  onDelete: (id: string) => Promise<void> | void;
  onUpdate: (id: string, note: string, clothingItem: ClothingItem, kind?: 'offer_like' | 'offer_dislike') => Promise<void> | void;
}) {
  const [selected, setSelected] = useState<TasteSample | null>(null);
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  const runAction = async (action: () => Promise<void> | void, closeAfterSuccess: boolean) => {
    setActionPending(true);
    setActionError(null);
    try {
      await action();
      if (closeAfterSuccess) setSelected(null);
    } catch (error) {
      setActionError(error);
    } finally {
      setActionPending(false);
    }
  };

  return (
    <>
      <div className="image-grid">
        {onAdd && (
          <button className="image-tile image-tile--add" onClick={onAdd} title="Add photos">
            <span>+</span>
          </button>
        )}
        {samples.map((sample) => {
          const image = sample.image_urls[0];
          return (
            <div
              key={sample.id}
              className="image-tile"
              role="button"
              tabIndex={0}
              aria-label={`Open ${sample.title || 'item'} details`}
              onClick={() => setSelected(sample)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelected(sample); }
              }}
              title={sample.note || undefined}
            >
              {image
                ? <img src={image} alt="" />
                : <div className="image-tile__placeholder">?</div>}
              {pendingDeleteId === sample.id ? (
                <span className="image-tile__delete-confirm" onClick={(e) => e.stopPropagation()}>
                  <button
                    className="image-tile__delete-confirm-yes"
                    onClick={(e) => { e.stopPropagation(); setPendingDeleteId(null); void runAction(() => onDelete(sample.id), false); }}
                    disabled={actionPending}
                    title="Confirm remove"
                  >✓</button>
                  <button
                    className="image-tile__delete-confirm-no"
                    onClick={(e) => { e.stopPropagation(); setPendingDeleteId(null); }}
                    title="Cancel"
                  >✕</button>
                </span>
              ) : (
                <button
                  className="image-tile__delete"
                  onClick={(e) => { e.stopPropagation(); setPendingDeleteId(sample.id); }}
                  title="Remove"
                  disabled={actionPending}
                >
                  ×
                </button>
              )}
            </div>
          );
        })}
        {!samples.length && !onAdd && (
          <p className="muted" style={{ fontSize: '0.88rem', padding: '0.5rem 0' }}>None yet.</p>
        )}
      </div>
      {!selected && <ErrorText error={actionError} prefix="Sample update failed" />}

      {selected && (
        <SampleDetailModal
          sample={selected}
          onClose={() => setSelected(null)}
          onUpdate={(note, clothingItem, kind) => {
            void runAction(() => onUpdate(selected.id, note, clothingItem, kind), true);
          }}
          onDelete={() => {
            void runAction(() => onDelete(selected.id), true);
          }}
          isUpdating={actionPending}
          error={actionError}
        />
      )}
    </>
  );
}

// ─── Prompt diff ──────────────────────────────────────────────────────────────

type DiffToken = { value: string; type: 'same' | 'removed' | 'added' };

function wordDiff(oldText: string, newText: string): DiffToken[] {
  // Keep whitespace in the output so the prompts retain their original wrapping,
  // but compare individual non-whitespace chunks instead of whole lines.
  const oldTokens = oldText.match(/\s+|\S+/g) ?? [];
  const newTokens = newText.match(/\s+|\S+/g) ?? [];
  const width = newTokens.length + 1;
  const lengths = new Uint32Array((oldTokens.length + 1) * width);

  for (let oldIndex = oldTokens.length - 1; oldIndex >= 0; oldIndex -= 1) {
    for (let newIndex = newTokens.length - 1; newIndex >= 0; newIndex -= 1) {
      const index = oldIndex * width + newIndex;
      lengths[index] = oldTokens[oldIndex] === newTokens[newIndex]
        ? lengths[(oldIndex + 1) * width + newIndex + 1] + 1
        : Math.max(lengths[(oldIndex + 1) * width + newIndex], lengths[oldIndex * width + newIndex + 1]);
    }
  }

  const result: DiffToken[] = [];
  let oldIndex = 0;
  let newIndex = 0;
  while (oldIndex < oldTokens.length && newIndex < newTokens.length) {
    if (oldTokens[oldIndex] === newTokens[newIndex]) {
      result.push({ value: oldTokens[oldIndex], type: 'same' });
      oldIndex += 1;
      newIndex += 1;
    } else if (lengths[(oldIndex + 1) * width + newIndex] >= lengths[oldIndex * width + newIndex + 1]) {
      result.push({ value: oldTokens[oldIndex], type: 'removed' });
      oldIndex += 1;
    } else {
      result.push({ value: newTokens[newIndex], type: 'added' });
      newIndex += 1;
    }
  }
  while (oldIndex < oldTokens.length) result.push({ value: oldTokens[oldIndex++], type: 'removed' });
  while (newIndex < newTokens.length) result.push({ value: newTokens[newIndex++], type: 'added' });
  return result;
}

function PromptDiff({
  oldPrompt,
  newPrompt,
  oldCharacterCount,
  oldTokenCount,
  newCharacterCount,
  newTokenCount,
  tokenizer,
}: {
  oldPrompt: string;
  newPrompt: string;
  oldCharacterCount?: number | null;
  oldTokenCount?: number | null;
  newCharacterCount?: number | null;
  newTokenCount?: number | null;
  tokenizer: string;
}) {
  const tokens = wordDiff(oldPrompt, newPrompt);
  const changedCount = tokens.filter((token) => token.type !== 'same' && /\S/.test(token.value)).length;
  const oldTokens = tokens.filter((token) => token.type !== 'added');
  const newTokens = tokens.filter((token) => token.type !== 'removed');
  return (
    <div style={{ margin: '0.75rem 0 0', maxHeight: '30rem', overflow: 'auto' }}>
      {oldCharacterCount != null && oldTokenCount != null && newCharacterCount != null && newTokenCount != null && (
        <p className="muted" style={{ margin: '0 0 0.5rem', fontSize: '0.82rem' }}>
          Old: {oldCharacterCount.toLocaleString()} chars · {oldTokenCount.toLocaleString()} tokens
          {' · '}New: {newCharacterCount.toLocaleString()} chars · {newTokenCount.toLocaleString()} tokens
          {' · '}{tokenizer}
        </p>
      )}
      {changedCount === 0 ? (
        <p className="muted" style={{ margin: 0 }}>No changes in taste prompt.</p>
      ) : (
        <>
          <p className="muted" style={{ margin: '0 0 0.5rem', fontSize: '0.82rem' }}>{changedCount} word{changedCount !== 1 ? 's' : ''} changed</p>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem', fontFamily: 'monospace' }}>
            <tbody>
              <tr>
                <td style={{ padding: '8px', whiteSpace: 'pre-wrap', verticalAlign: 'top', width: '50%', borderRight: '1px solid var(--border)' }}>
                  {oldTokens.map((token, index) => (
                    <span key={index} style={token.type === 'removed' ? { background: 'var(--danger-bg)', textDecoration: 'line-through', opacity: 0.7 } : undefined}>
                      {token.value}
                    </span>
                  ))}
                </td>
                <td style={{ padding: '8px', whiteSpace: 'pre-wrap', verticalAlign: 'top', width: '50%' }}>
                  {newTokens.map((token, index) => (
                    <span key={index} style={token.type === 'added' ? { background: 'var(--ok-bg)' } : undefined}>
                      {token.value}
                    </span>
                  ))}
                </td>
              </tr>
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

// ─── Draft searches ───────────────────────────────────────────────────────────

type DraftFieldKey = 'query';
type DiffStatus = 'added' | 'changed' | 'removed' | 'unchanged';

type FilterDiff = {
  key: string;
  label: string;
  status: DiffStatus;
  current: SearchFilter[];
  draft: SearchFilter[];
};

function filtersEqual(left: SearchFilter[], right: SearchFilter[]): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function filterDiffs(current: SearchFilter[], draft: SearchFilter[]): FilterDiff[] {
  const currentGroups = groupFilters(current);
  const draftGroups = groupFilters(draft);
  const keys = Array.from(new Set([...currentGroups.keys(), ...draftGroups.keys()]));
  return keys.map((key) => {
    const currentFilters = currentGroups.get(key) ?? [];
    const draftFilters = draftGroups.get(key) ?? [];
    const status: DiffStatus = !currentFilters.length
      ? 'added'
      : !draftFilters.length
        ? 'removed'
        : filtersEqual(currentFilters, draftFilters)
          ? 'unchanged'
          : 'changed';
    return {
      key,
      label: filterGroupLabel(key, draftFilters.length ? draftFilters : currentFilters),
      status,
      current: currentFilters,
      draft: draftFilters,
    };
  });
}

function withoutDraftPriceFilters(filters: SearchFilter[]): SearchFilter[] {
  return filters.filter((filter) => !isPriceCeilingFilter(filter));
}

function defaultDraftSelection(search: SearchRecord, draft: GeneratedSearchDraft): {
  fields: Set<DraftFieldKey>;
  filterGroups: Set<string>;
} {
  const fields = new Set<DraftFieldKey>();
  if (search.query !== draft.query) fields.add('query');
  const filterGroups = new Set(
    filterDiffs(search.filters, withoutDraftPriceFilters(draft.filters))
      .filter((diff) => diff.key !== 'size' && diff.key !== 'price:range')
      .filter((diff) => diff.status === 'added' || diff.status === 'changed')
      .map((diff) => diff.key),
  );
  return { fields, filterGroups };
}

function mergeDraftIntoSearch(
  search: SearchRecord,
  draft: GeneratedSearchDraft,
  selectedFields: Set<DraftFieldKey>,
  selectedFilterGroups: Set<string>,
): SearchUpdatePayload {
  const draftGroups = groupFilters(withoutDraftPriceFilters(draft.filters));
  let filters = [...search.filters];
  for (const key of selectedFilterGroups) {
    filters = replaceFilterGroup(filters, key, draftGroups.get(key) ?? []);
  }
  return {
    clothing_item: search.clothing_item,
    query: selectedFields.has('query') ? draft.query : search.query,
    region: search.region,
    filters,
    alert_threshold: search.alert_threshold,
    enabled: search.enabled,
  };
}

function toggleSetValue<T>(set: Set<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

function statusLabel(status: DiffStatus): string {
  if (status === 'added') return 'added';
  if (status === 'removed') return 'removed';
  if (status === 'changed') return 'changed';
  return 'same';
}

function DraftCompareModal({
  draft,
  itemProfile,
  search,
  learningSnapshot,
  onClose,
  onApply,
  isPending,
  error,
}: {
  draft: GeneratedSearchDraft;
  itemProfile: ClothingItemTasteProfile | null;
  search: SearchRecord;
  learningSnapshot: LearningSnapshot | null;
  onClose: () => void;
  onApply: (payload: SearchUpdatePayload) => void;
  isPending: boolean;
  error: unknown;
}) {
  const diffs = useMemo(
    () => filterDiffs(search.filters, withoutDraftPriceFilters(draft.filters))
      .filter((diff) => diff.key !== 'size' && diff.key !== 'price:range'),
    [search.filters, draft.filters],
  );
  const [selectedFields, setSelectedFields] = useState<Set<DraftFieldKey>>(
    () => defaultDraftSelection(search, draft).fields,
  );
  const [selectedFilterGroups, setSelectedFilterGroups] = useState<Set<string>>(
    () => defaultDraftSelection(search, draft).filterGroups,
  );
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalDismiss(dialogRef, onClose, isPending);

  const hasSelection = selectedFields.size > 0 || selectedFilterGroups.size > 0;
  const fieldRows: { key: DraftFieldKey; label: string; current: string; draft: string }[] = [
    { key: 'query', label: 'Vinted query', current: search.query || '—', draft: draft.query || '—' },
  ];
  const oldItemProfile = learningSnapshot?.old_taste_profile?.item_profiles[draft.clothing_item] ?? null;
  const newItemProfile = learningSnapshot?.new_taste_profile?.item_profiles[draft.clothing_item] ?? itemProfile;
  const profileRows: { label: string; previous: string; current: string; judgeUse: string }[] = newItemProfile ? [
    {
      label: 'Transparency labels',
      previous: oldItemProfile?.transparency_labels.length ? oldItemProfile.transparency_labels.join(' · ') : 'None',
      current: newItemProfile.transparency_labels.length ? newItemProfile.transparency_labels.join(' · ') : 'None',
      judgeUse: 'For you only — quick labels that summarise the profile; not sent to the judge.',
    },
    {
      label: 'Instant alerts',
      previous: oldItemProfile?.instant_alert_examples.length ? oldItemProfile.instant_alert_examples.join(' · ') : 'None',
      current: newItemProfile.instant_alert_examples.length ? newItemProfile.instant_alert_examples.join(' · ') : 'None',
      judgeUse: 'Fallback positive calibration examples for the judge when there are no real liked listings yet.',
    },
    {
      label: 'Instant rejects',
      previous: oldItemProfile?.instant_reject_examples.length ? oldItemProfile.instant_reject_examples.join(' · ') : 'None',
      current: newItemProfile.instant_reject_examples.length ? newItemProfile.instant_reject_examples.join(' · ') : 'None',
      judgeUse: 'Fallback negative calibration examples for the judge when there are no real disliked listings yet.',
    },
  ] : [];

  return createPortal(
    <div className="modal-overlay" onClick={() => { if (!isPending) onClose(); }}>
      <div
        className="modal draft-compare-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Compare generated draft — ${clothingItemLabel(draft.clothing_item)}`}
        ref={dialogRef}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <div>
            <p className="eyebrow">Compare generated draft</p>
            <h3>{clothingItemLabel(draft.clothing_item)}</h3>
          </div>
          <button className="modal-close secondary" onClick={onClose} disabled={isPending}>×</button>
        </div>

        <p className="muted" style={{ marginTop: 0 }}>{draft.rationale || 'No rationale provided.'}</p>

        <div className="draft-diff-list">
          {fieldRows.map((row) => {
            const changed = row.current !== row.draft;
            return (
              <label className={`draft-diff-row ${changed ? 'changed' : 'unchanged'}`} key={row.key}>
                <input
                  type="checkbox"
                  checked={selectedFields.has(row.key)}
                  disabled={!changed || isPending}
                  onChange={() => setSelectedFields((current) => toggleSetValue(current, row.key))}
                />
                <div className="draft-diff-row__body">
                  <div className="row-between">
                    <strong>{row.label}</strong>
                    <span className={`diff-badge ${changed ? 'changed' : 'unchanged'}`}>{changed ? 'changed' : 'same'}</span>
                  </div>
                  <div className="draft-diff-columns">
                    <p><span>Current</span>{row.current}</p>
                    <p><span>Draft</span>{row.draft}</p>
                  </div>
                </div>
              </label>
            );
          })}

          {diffs.map((diff) => (
            <label className={`draft-diff-row ${diff.status}`} key={diff.key}>
              <input
                type="checkbox"
                checked={selectedFilterGroups.has(diff.key)}
                disabled={diff.status === 'unchanged' || isPending}
                onChange={() => setSelectedFilterGroups((current) => toggleSetValue(current, diff.key))}
              />
              <div className="draft-diff-row__body">
                <div className="row-between">
                  <strong>{diff.label}</strong>
                  <span className={`diff-badge ${diff.status}`}>{statusLabel(diff.status)}</span>
                </div>
                <div className="draft-diff-columns">
                  <p><span>Current</span>{formatSearchFilters(diff.current)}</p>
                  <p><span>Draft</span>{formatSearchFilters(diff.draft)}</p>
                </div>
              </div>
            </label>
          ))}
        </div>

        {profileRows.length ? (
          <section className="draft-profile-fields" aria-label="Taste-profile calibration comparison">
            <p className="muted" style={{ margin: 0, gridColumn: '1 / -1', fontSize: '0.82rem' }}>
              Taste-profile calibration from the last recompute.
              {!oldItemProfile ? ' The previous profile was not saved, so only the new values are available.' : ''}
            </p>
            {profileRows.map((row) => {
              const changed = row.previous !== row.current;
              return (
                <div className="draft-profile-field" key={row.label}>
                  <div className="row-between">
                    <span>{row.label}</span>
                    <span className={`diff-badge ${changed ? 'changed' : 'unchanged'}`}>{changed ? 'changed' : 'same'}</span>
                  </div>
                  <div className="draft-diff-columns">
                    <p><span>Previous</span>{row.previous}</p>
                    <p><span>New</span>{row.current}</p>
                  </div>
                  <p className="muted" style={{ marginBottom: 0, fontSize: '0.78rem' }}>{row.judgeUse}</p>
                </div>
              );
            })}
          </section>
        ) : null}

        {learningSnapshot?.old_prompt && learningSnapshot?.new_prompt && (
          <details className="details-block" style={{ marginTop: '1rem' }}>
            <summary>Prompt diff (last recompute: {learningSnapshot.reason})</summary>
            <PromptDiff
              oldPrompt={learningSnapshot.old_prompt}
              newPrompt={learningSnapshot.new_prompt}
              oldCharacterCount={learningSnapshot.old_prompt_character_count}
              oldTokenCount={learningSnapshot.old_prompt_token_count}
              newCharacterCount={learningSnapshot.new_prompt_character_count}
              newTokenCount={learningSnapshot.new_prompt_token_count}
              tokenizer={learningSnapshot.prompt_tokenizer}
            />
          </details>
        )}

        <div className="button-row" style={{ marginTop: '1rem' }}>
          <button
            onClick={() => onApply(mergeDraftIntoSearch(search, draft, selectedFields, selectedFilterGroups))}
            disabled={isPending || !hasSelection}
          >
            {isPending ? 'Applying…' : 'Apply selected changes'}
          </button>
          <button className="secondary" onClick={onClose} disabled={isPending}>Cancel</button>
        </div>
        <ErrorText error={error} prefix="Draft apply failed" />
      </div>
    </div>,
    document.body,
  );
}

function DraftSearches({
  drafts,
  allDrafts,
  itemProfile,
  learningSnapshot,
}: {
  drafts: GeneratedSearchDraft[];
  allDrafts: GeneratedSearchDraft[];
  itemProfile: ClothingItemTasteProfile | null;
  learningSnapshot: LearningSnapshot | null;
}) {
  const queryClient = useQueryClient();
  const searchesQuery = useQuery({ queryKey: queryKeys.searches, queryFn: api.getSearches });
  const [selectedDraft, setSelectedDraft] = useState<GeneratedSearchDraft | null>(null);
  const updateMutation = useMutation({
    mutationFn: ({ searchId, payload }: { searchId: string; payload: SearchUpdatePayload }) => api.updateSearch(searchId, payload),
    onSuccess: () => {
      setSelectedDraft(null);
      queryClient.invalidateQueries({ queryKey: queryKeys.searches });
    },
  });

  const searches = searchesQuery.data ?? [];
  const quickApplyAllMutation = useMutation({
    mutationFn: async () => {
      const updates = allDrafts.flatMap((draft) => {
        const search = searches.find((item) => item.clothing_item === draft.clothing_item);
        if (!search) return [];
        const { fields, filterGroups } = defaultDraftSelection(search, draft);
        if (!fields.size && !filterGroups.size) return [];
        return [{ searchId: search.id, payload: mergeDraftIntoSearch(search, draft, fields, filterGroups) }];
      });
      await Promise.all(updates.map(({ searchId, payload }) => api.updateSearch(searchId, payload)));
      return updates.length;
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.searches }),
  });
  const quickApplicableCount = allDrafts.filter((draft) => {
    const search = searches.find((item) => item.clothing_item === draft.clothing_item);
    if (!search) return false;
    const { fields, filterGroups } = defaultDraftSelection(search, draft);
    return fields.size > 0 || filterGroups.size > 0;
  }).length;
  const selectedSearch = selectedDraft
    ? searches.find((search) => search.clothing_item === selectedDraft.clothing_item) ?? null
    : null;

  if (!drafts.length && !allDrafts.length) return null;

  return (
    <article className="card" style={{ marginTop: '1rem' }}>
      <div className="row-between" style={{ marginBottom: '1rem' }}>
        <h4 style={{ margin: 0 }}>Generated search drafts</h4>
        <button
          className="secondary"
          onClick={() => quickApplyAllMutation.mutate()}
          disabled={
            searchesQuery.isLoading
            || updateMutation.isPending
            || quickApplyAllMutation.isPending
            || quickApplicableCount === 0
          }
          title={quickApplicableCount ? 'Apply the same defaults preselected in every comparison.' : 'No preselected draft changes to apply.'}
        >
          {quickApplyAllMutation.isPending ? 'Applying all…' : 'Quick apply all'}
        </button>
      </div>
      <div className="stack compact">
        {drafts.map((draft) => (
            <div key={draft.id} className="row-between">
              <div>
                <strong style={{ fontSize: '0.95rem' }}>{clothingItemLabel(draft.clothing_item)}</strong>
                <p className="muted" style={{ fontSize: '0.85rem', margin: '0.2rem 0 0' }}>
                  {draft.query} · {draft.region} · {draft.rationale}
                </p>
                <p className="muted" style={{ fontSize: '0.8rem', margin: '0.2rem 0 0' }}>
                  Filters: {formatSearchFilters(withoutDraftPriceFilters(draft.filters))}
                </p>
              </div>
              {(() => {
                const existing = searches.find((search) => search.clothing_item === draft.clothing_item);
                const selection = existing ? defaultDraftSelection(existing, draft) : null;
                const quickApplicable = !!selection && (selection.fields.size > 0 || selection.filterGroups.size > 0);
                return (
                  <div className="button-row" style={{ flexShrink: 0 }}>
                    <button
                      className="secondary"
                      onClick={() => {
                        if (!existing || !selection) return;
                        updateMutation.mutate({
                          searchId: existing.id,
                          payload: mergeDraftIntoSearch(existing, draft, selection.fields, selection.filterGroups),
                        });
                      }}
                      disabled={searchesQuery.isLoading || !existing || !quickApplicable || updateMutation.isPending || quickApplyAllMutation.isPending}
                      title={!existing ? 'Canonical search for this clothing bucket is missing.' : !quickApplicable ? 'No preselected changes to apply.' : 'Apply the same changes preselected in Compare & apply.'}
                    >
                      {updateMutation.isPending ? 'Applying…' : 'Quick apply'}
                    </button>
                    <button
                      className="secondary"
                      onClick={() => setSelectedDraft(draft)}
                      disabled={searchesQuery.isLoading || !existing || updateMutation.isPending || quickApplyAllMutation.isPending}
                      title={existing ? undefined : 'Canonical search for this clothing bucket is missing.'}
                    >
                      Compare & apply
                    </button>
                  </div>
                );
              })()}
            </div>
          ))}
      </div>
      <ErrorText error={searchesQuery.error} prefix="Searches failed to load" />
      <ErrorText error={quickApplyAllMutation.error} prefix="Quick apply all failed" />
      {selectedDraft && selectedSearch ? (
        <DraftCompareModal
          draft={selectedDraft}
          itemProfile={itemProfile}
          search={selectedSearch}
          learningSnapshot={learningSnapshot}
          onClose={() => setSelectedDraft(null)}
          onApply={(payload) => updateMutation.mutate({ searchId: selectedSearch.id, payload })}
          isPending={updateMutation.isPending}
          error={updateMutation.error}
        />
      ) : null}
    </article>
  );
}

// ─── Blocked brands ────────────────────────────────────────────────────────────

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

function BrandSearchInput({
  blockedBrands,
  onAdd,
}: {
  blockedBrands: string[];
  onAdd: (title: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const debouncedQuery = useDebouncedValue(query.trim(), 300);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const suggestionsQuery = useQuery({
    queryKey: queryKeys.vintedBrandSearch(debouncedQuery),
    queryFn: () => api.searchVintedBrands(debouncedQuery),
    enabled: debouncedQuery.length >= 2,
  });

  useEffect(() => {
    const handleClick = (event: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const blockedLower = useMemo(() => new Set(blockedBrands.map((b) => b.toLowerCase())), [blockedBrands]);
  const suggestions = (suggestionsQuery.data ?? []).filter((option) => !blockedLower.has(option.title.toLowerCase()));

  const pick = (title: string) => {
    onAdd(title);
    setQuery('');
    setOpen(false);
  };

  return (
    <div className="brand-search" ref={wrapperRef}>
      <input
        value={query}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            if (suggestions.length) pick(suggestions[0].title);
            else if (query.trim()) pick(query.trim());
          }
          if (e.key === 'Escape') setOpen(false);
        }}
        placeholder="Search brands on Vinted…"
      />
      {open && debouncedQuery.length >= 2 && (
        <div className="brand-search__suggestions">
          {suggestionsQuery.isLoading ? (
            <p className="brand-search__empty">Searching…</p>
          ) : suggestionsQuery.isError ? (
            <p className="brand-search__empty">
              Vinted brand lookup failed — {suggestionsQuery.error instanceof Error ? suggestionsQuery.error.message : String(suggestionsQuery.error)}
              {query.trim() ? ` Press Enter to block “${query.trim()}” anyway.` : ''}
            </p>
          ) : suggestions.length ? (
            suggestions.map((option) => (
              <button
                type="button"
                className="brand-search__suggestion"
                key={option.id}
                onMouseDown={(e) => { e.preventDefault(); pick(option.title); }}
              >
                {option.title}
              </button>
            ))
          ) : (
            <p className="brand-search__empty">
              No matching brand found on Vinted. Press Enter to block “{query.trim()}” anyway.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function BlockedBrandsSection({
  brands,
  onAdd,
  onRemove,
  isSaving,
  error,
}: {
  brands: string[];
  onAdd: (title: string) => void;
  onRemove: (title: string) => void;
  isSaving: boolean;
  error: unknown;
}) {
  return (
    <TasteSection eyebrow="Filters" title="Blocked brands">
      <article className="card">
        <p className="muted" style={{ marginTop: 0 }}>
          Offers from these brands are excluded before scoring — they're never fetched into candidates or sent to the AI judge.
        </p>
        <BrandSearchInput blockedBrands={brands} onAdd={onAdd} />
        <div className="tag-row" style={{ marginTop: '1rem' }}>
          {brands.length ? (
            brands.map((brand) => (
              <button
                type="button"
                className="token-chip"
                key={brand}
                onClick={() => onRemove(brand)}
                disabled={isSaving}
                title={`Unblock ${brand}`}
              >
                {brand} <span aria-hidden="true">×</span>
              </button>
            ))
          ) : (
            <p className="muted" style={{ fontSize: '0.88rem', margin: 0 }}>No brands blocked yet.</p>
          )}
        </div>
        <ErrorText error={error} prefix="Blocked brands update failed" />
      </article>
    </TasteSection>
  );
}

// ─── Section wrapper ──────────────────────────────────────────────────────────

function TasteSection({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="taste-section">
      <div className="taste-section__header">
        <p className="eyebrow">{eyebrow}</p>
        <h3>{title}</h3>
      </div>
      {children}
    </div>
  );
}

function ProfileHeading({ children }: { children: React.ReactNode }) {
  return <h4 className="profile-heading">{children}</h4>;
}

function ProfileBullets({ items }: { items: string[] }) {
  if (!items.length) return <p className="muted" style={{ fontSize: '0.88rem', margin: 0 }}>None yet.</p>;
  return (
    <ul className="profile-bullets">
      {items.map((item, index) => <li key={index}>{item}</li>)}
    </ul>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function MyTastePage() {
  const queryClient = useQueryClient();
  const tasteQuery = useQuery({
    queryKey: queryKeys.taste,
    queryFn: api.getTaste,
    // Poll while a recompute is running (possibly started elsewhere) so the UI
    // reflects completion without a manual refresh.
    refetchInterval: (query) => (query.state.data?.recompute_state?.status === 'running' ? 3000 : false),
  });
  const blockedBrandsQuery = useQuery({ queryKey: queryKeys.blockedBrands, queryFn: api.getBlockedBrands });
  const updateBlockedBrandsMutation = useMutation({
    mutationFn: api.updateBlockedBrands,
    onSuccess: (result) => queryClient.setQueryData(queryKeys.blockedBrands, result),
  });
  const [uploadOpen, setUploadOpen] = useState(false);
  const [offerOpen, setOfferOpen] = useState(false);
  const [uploadTargetItem, setUploadTargetItem] = useState<ClothingItem>('hosen');
  const [zipImportNotice, setZipImportNotice] = useState<string | null>(null);
  const zipInputRef = useRef<HTMLInputElement>(null);
  const [manualNote, setManualNote] = useState('');
  const [lastCost, setLastCost] = useState<number | null>(null);
  const [lastObservationCache, setLastObservationCache] = useState<{
    total_image_inputs: number;
    cached_observations: number;
    fresh_observations: number;
    observation_provider: 'local' | 'openai';
    observation_model: string;
  } | null>(null);
  const manualNoteDirty = useRef(false);
  const [noteDirty, setNoteDirty] = useState(false);
  const setManualNoteDirty = (value: boolean) => { manualNoteDirty.current = value; setNoteDirty(value); };
  useUnsavedGuard(noteDirty);

  useEffect(() => {
    if (!tasteQuery.data || manualNoteDirty.current) return;
    setManualNote(tasteQuery.data.manual_note.text);
  }, [tasteQuery.data?.manual_note.text]);

  const [selectedProfileItem, setSelectedProfileItem] = useState<ClothingItem>('hosen');

  const judgmentPromptQuery = useQuery({
    queryKey: queryKeys.judgmentPrompt(selectedProfileItem),
    queryFn: () => api.getJudgmentPrompt(selectedProfileItem),
    enabled: !!tasteQuery.data?.taste_profile,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.taste });
    queryClient.invalidateQueries({ queryKey: queryKeys.candidates });
  };

  const uploadMutation = useMutation({
    mutationFn: ({ file, note, clothingItem }: { file: File; note: string; clothingItem: ClothingItem }) =>
      api.uploadWardrobeImage(file, note, clothingItem),
    onSuccess: () => invalidate(),
  });

  const zipImportMutation = useMutation({
    mutationFn: (file: File) => api.importWardrobeZip(file),
    onSuccess: (result: WardrobeZipImportResult) => {
      invalidate();
      const n = result.imported.length;
      const s = result.skipped.length;
      setZipImportNotice(s > 0 ? `Imported ${n} photo${n !== 1 ? 's' : ''} (${s} skipped)` : `Imported ${n} photo${n !== 1 ? 's' : ''}`);
      setTimeout(() => setZipImportNotice(null), 5000);
    },
  });

  const updateSampleMutation = useMutation({
    mutationFn: ({ sampleId, note, clothingItem, kind }: { sampleId: string; note: string; clothingItem: ClothingItem; kind?: 'offer_like' | 'offer_dislike' }) =>
      api.updateTasteSample(sampleId, { note, clothing_item: clothingItem, ...(kind ? { kind } : {}) }),
    onSuccess: invalidate,
  });
  const deleteSampleMutation = useMutation({ mutationFn: api.deleteTasteSample, onSuccess: invalidate });
  const addOfferMutation = useMutation({
    mutationFn: (payload: { vinted_url: string; kind: 'offer_like' | 'offer_dislike'; clothing_item: ClothingItem; note: string }) =>
      api.addTasteOffer(payload),
    onSuccess: invalidate,
  });
  const manualNoteMutation = useMutation({
    mutationFn: api.updateTasteManualNote,
    onSuccess: (_result, submittedNote) => {
      if (manualNote === submittedNote) {
        setManualNoteDirty(false);
      }
      invalidate();
    },
  });
  const recomputeMutation = useMutation({
    mutationFn: api.recomputeTaste,
    onSuccess: (result) => {
      setLastCost(result.cost_usd);
      setLastObservationCache(result.observation_cache);
      queryClient.setQueryData(queryKeys.taste, result.snapshot);
      queryClient.invalidateQueries({ queryKey: queryKeys.searches });
      queryClient.invalidateQueries({ queryKey: queryKeys.costs });
    },
    onError: () => {
      // The request may have died client-side (proxy timeout / network blip) while the
      // synchronous server-side recompute keeps running. Refetch so the snapshot's
      // recompute_state takes over driving the polling/banner, instead of leaving the UI
      // stuck on "Recompute failed" until the user re-clicks and hits an opaque 409.
      queryClient.invalidateQueries({ queryKey: queryKeys.taste });
    },
  });
  const cancelRecomputeMutation = useMutation({
    mutationFn: api.cancelTasteRecompute,
    onSuccess: (snapshot) => queryClient.setQueryData(queryKeys.taste, snapshot),
  });

  const grouped = useMemo(() => {
    const samples = tasteQuery.data?.samples ?? [];
    return {
      wardrobe: samples.filter((s) => s.kind === 'wardrobe'),
      liked: samples.filter((s) => s.kind === 'offer_like'),
      disliked: samples.filter((s) => s.kind === 'offer_dislike'),
      noted: samples.filter((s) => s.kind === 'offer_note'),
    };
  }, [tasteQuery.data?.samples]);

  const wardrobeByItem = useMemo(() => {
    const entries = clothingItems.map((item) => [
      item.value,
      grouped.wardrobe.filter((sample) => sample.clothing_item === item.value),
    ] as const);
    return Object.fromEntries(entries) as Record<ClothingItem, TasteSample[]>;
  }, [grouped.wardrobe]);

  if (tasteQuery.isLoading) return <p>Loading taste…</p>;
  if (tasteQuery.isError || !tasteQuery.data) return <ErrorText error={tasteQuery.error ?? 'Could not load taste.'} />;

  const taste = tasteQuery.data;
  const profile = taste.taste_profile;
  const selectedItemProfile = profile?.item_profiles?.[selectedProfileItem] ?? null;
  // Item-specific only: a tab without its own profile shows empties rather than
  // leaking global / other-bucket signals as if they were item-specific.
  const selectedRubric = Object.entries(selectedItemProfile?.scoring_rubric ?? {});
  const selectedLikes = selectedItemProfile?.likes ?? [];
  const selectedDislikes = selectedItemProfile?.dislikes_or_penalties ?? [];
  const selectedAlertExamples = selectedItemProfile?.instant_alert_examples ?? [];
  const selectedRejectExamples = selectedItemProfile?.instant_reject_examples ?? [];
  const selectedDrafts = selectedItemProfile?.generated_search
    ? [selectedItemProfile.generated_search]
    : (profile?.generated_searches?.filter((draft) => draft.clothing_item === selectedProfileItem) ?? []);
  const allDrafts = profile?.generated_searches ?? [];

  const recomputeState = taste.recompute_state;
  const recomputeRunning = recomputeState?.status === 'running';
  const recomputeBusy = recomputeRunning || recomputeMutation.isPending;
  // Prefer the freshest local result, fall back to the persisted snapshot value
  // (only meaningful once a recompute has actually run).
  const displayCost = lastCost ?? (taste.last_recomputed_at ? recomputeState?.last_cost_usd ?? null : null);
  const recomputeConflict = recomputeMutation.error instanceof ApiError && recomputeMutation.error.status === 409;

  return (
    <section>
      {/* Page header */}
      <div className="page-header">
        <div>
          <p className="eyebrow">My taste</p>
          <h2>Clothes I like</h2>
        </div>
        <div className="button-row">
          <button onClick={() => recomputeMutation.mutate()} disabled={recomputeBusy}>
            {recomputeBusy ? 'Recomputing…' : 'Recompute taste profile'}
          </button>
          {recomputeRunning ? (
            <button
              className="secondary"
              onClick={() => cancelRecomputeMutation.mutate()}
              disabled={cancelRecomputeMutation.isPending}
            >
              {cancelRecomputeMutation.isPending ? 'Cancelling…' : 'Cancel'}
            </button>
          ) : null}
        </div>
      </div>
      {/* While a recompute is running (incl. one the server kept going after a client-side
          request failure), show only the running banner — a stale client error or 409 here
          would falsely read as "failed". Cancel only force-clears the DB claim (see
          cancel_recompute docstring) — if the stuck call is still alive server-side it can still
          take a while to actually stop, but the button unblocks a fresh attempt immediately. */}
      {recomputeRunning ? (
        <p className="muted" style={{ marginTop: '0.25rem', fontSize: '0.85rem' }}>
          A recompute is running… this view updates automatically when it finishes. Stuck for a
          while? Cancel it and try again.
        </p>
      ) : recomputeConflict ? (
        <ErrorText error="A recompute is already running." prefix="Recompute failed" />
      ) : recomputeMutation.error ? (
        <ErrorText error={recomputeMutation.error} prefix="Recompute failed" />
      ) : cancelRecomputeMutation.error ? (
        <ErrorText error={cancelRecomputeMutation.error} prefix="Cancel failed" />
      ) : recomputeState?.status === 'failed' ? (
        <ErrorText error={recomputeState.error ?? 'Last recompute failed.'} prefix="Recompute failed" />
      ) : recomputeState?.status === 'cancelled' ? (
        <p className="muted" style={{ marginTop: '0.25rem', fontSize: '0.85rem' }}>Last recompute was cancelled.</p>
      ) : null}

      {/* Stats row */}
      <div className="card-grid" style={{ marginBottom: '2.5rem' }}>
        <article className="card stat-card">
          <span>Last update</span>
          <strong style={{ fontSize: '1rem' }}>
            {formatBerlinDateTime(taste.last_recomputed_at, 'never')}
          </strong>
        </article>
        <article className="card stat-card">
          <span>Changed samples</span>
          <strong>{taste.dirty_counts.new_or_changed_samples}</strong>
        </article>
        <article className="card stat-card">
          <span>Pos / Neg changed</span>
          <strong>
            {taste.dirty_counts.new_or_changed_positive_samples} / {taste.dirty_counts.new_or_changed_negative_samples}
          </strong>
        </article>
        <article className="card stat-card">
          <span>Last recompute cost</span>
          <strong>{displayCost === null ? 'n/a' : fmtUsd(displayCost)}</strong>
        </article>
        <article className="card stat-card">
          <span>Image observations</span>
          <strong style={{ fontSize: '1rem' }}>
            {lastObservationCache
              ? `${lastObservationCache.cached_observations} cached / ${lastObservationCache.fresh_observations} fresh`
              : 'n/a'}
          </strong>
          {lastObservationCache && (
            <span className="muted" style={{ fontSize: '0.78rem' }}>
              {lastObservationCache.observation_provider} · {lastObservationCache.observation_model || 'n/a'} · {lastObservationCache.total_image_inputs} images
            </span>
          )}
        </article>
      </div>

      {/* Wardrobe */}
      <TasteSection eyebrow="Reference" title="My wardrobe">
        <div className="button-row" style={{ marginBottom: '1rem' }}>
          <input
            ref={zipInputRef}
            type="file"
            accept=".zip"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) zipImportMutation.mutate(f);
              e.target.value = '';
            }}
          />
          <button
            className="secondary"
            onClick={() => zipInputRef.current?.click()}
            disabled={zipImportMutation.isPending}
          >
            {zipImportMutation.isPending ? 'Importing…' : 'Import ZIP'}
          </button>
          {zipImportNotice && <span className="muted" style={{ fontSize: '0.85rem' }}>{zipImportNotice}</span>}
          <ErrorText error={zipImportMutation.error} prefix="Import failed" />
        </div>
        <div className="stack">
          {clothingItems.map((item) => (
            <article className="card" key={item.value}>
              <div className="row-between" style={{ marginBottom: '0.75rem' }}>
                <div>
                  <h4 style={{ margin: 0 }}>{item.label}</h4>
                  <p className="muted" style={{ margin: '0.2rem 0 0', fontSize: '0.84rem' }}>{item.description}</p>
                </div>
                <span className="pill">{wardrobeByItem[item.value].length}</span>
              </div>
              <ImageGrid
                samples={wardrobeByItem[item.value]}
                onAdd={() => { setUploadTargetItem(item.value); setUploadOpen(true); }}
                onDelete={(id) => deleteSampleMutation.mutateAsync(id).then(() => undefined)}
                onUpdate={(id, note, clothingItem) =>
                  updateSampleMutation.mutateAsync({ sampleId: id, note, clothingItem }).then(() => undefined)}
              />
            </article>
          ))}
        </div>
      </TasteSection>

      {/* Taste note */}
      <TasteSection eyebrow="Instructions" title="Taste note">
        <article className="card">
          <textarea
            value={manualNote}
            onChange={(e) => { setManualNoteDirty(true); setManualNote(e.target.value); }}
            rows={6}
            placeholder="Describe your style, what you look for, what to avoid…"
          />
          <div className="button-row" style={{ marginTop: '1rem' }}>
            <button
              onClick={() => manualNoteMutation.mutate(manualNote)}
              disabled={manualNoteMutation.isPending}
            >
              {manualNoteMutation.isPending ? 'Saving…' : 'Save note'}
            </button>
            {taste.dirty_counts.manual_note_changed && (
              <span className="muted" style={{ fontSize: '0.85rem' }}>Changed since last recompute</span>
            )}
          </div>
          <ErrorText error={manualNoteMutation.error} prefix="Save failed" />
        </article>
      </TasteSection>

      {/* Blocked brands */}
      <BlockedBrandsSection
        brands={blockedBrandsQuery.data?.brands ?? []}
        isSaving={updateBlockedBrandsMutation.isPending}
        error={blockedBrandsQuery.error ?? updateBlockedBrandsMutation.error}
        onAdd={(title) => {
          const current = blockedBrandsQuery.data?.brands ?? [];
          if (current.some((brand) => brand.toLowerCase() === title.toLowerCase())) return;
          updateBlockedBrandsMutation.mutate([...current, title]);
        }}
        onRemove={(title) => {
          const current = blockedBrandsQuery.data?.brands ?? [];
          updateBlockedBrandsMutation.mutate(current.filter((brand) => brand !== title));
        }}
      />

      {/* Offer feedback */}
      <TasteSection eyebrow="Training data" title="Offer feedback">
        <div className="button-row" style={{ marginBottom: '1rem' }}>
          <button className="secondary" onClick={() => setOfferOpen(true)}>Add by URL</button>
        </div>
        <ErrorText error={addOfferMutation.error} prefix="Add offer failed" />
        <div className="stack">
          <article className="card">
            <div className="feedback-col-header feedback-col-header--liked">
              <span>Liked</span>
              <span className="feedback-col-count">{grouped.liked.length}</span>
            </div>
            <ImageGrid
              samples={grouped.liked}
              onDelete={(id) => deleteSampleMutation.mutateAsync(id).then(() => undefined)}
              onUpdate={(id, note, clothingItem, kind) =>
                updateSampleMutation.mutateAsync({ sampleId: id, note, clothingItem, kind }).then(() => undefined)}
            />
          </article>
          <article className="card">
            <div className="feedback-col-header feedback-col-header--disliked">
              <span>Disliked</span>
              <span className="feedback-col-count">{grouped.disliked.length}</span>
            </div>
            <ImageGrid
              samples={grouped.disliked}
              onDelete={(id) => deleteSampleMutation.mutateAsync(id).then(() => undefined)}
              onUpdate={(id, note, clothingItem, kind) =>
                updateSampleMutation.mutateAsync({ sampleId: id, note, clothingItem, kind }).then(() => undefined)}
            />
          </article>
          <article className="card">
            <div className="feedback-col-header">
              <span>Noted</span>
              <span className="feedback-col-count">{grouped.noted.length}</span>
            </div>
            <ImageGrid
              samples={grouped.noted}
              onDelete={(id) => deleteSampleMutation.mutateAsync(id).then(() => undefined)}
              onUpdate={(id, note, clothingItem) =>
                updateSampleMutation.mutateAsync({ sampleId: id, note, clothingItem }).then(() => undefined)}
            />
          </article>
        </div>
      </TasteSection>
      <ErrorText error={deleteSampleMutation.error ?? updateSampleMutation.error} prefix="Sample update failed" />

      {/* Taste profile */}
      <TasteSection eyebrow="AI output" title="Taste profile">
        <article className="card">
          <p style={{ margin: 0 }}>{profile?.summary ?? 'No taste profile generated yet.'}</p>
          <p className="muted" style={{ fontSize: '0.82rem', marginTop: '0.6rem' }}>
            Version {profile?.version ?? 0} · model {profile?.model ?? 'n/a'} · reasoning {profile?.reasoning_effort ?? 'n/a'}
          </p>
        </article>
        <div className="tabs" style={{ marginTop: '1rem' }}>
          {clothingItems.map((item) => (
            <button
              key={item.value}
              className={selectedProfileItem === item.value ? 'active' : ''}
              onClick={() => setSelectedProfileItem(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <DraftSearches
          drafts={selectedDrafts}
          allDrafts={allDrafts}
          itemProfile={selectedItemProfile}
          learningSnapshot={taste.latest_learning_snapshot}
        />
        {(selectedItemProfile?.taste_prompt || profile?.taste_prompt) && (
          <article className="card" style={{ marginTop: '1rem' }}>
            <details className="details-block" style={{ marginTop: 0, paddingTop: 0, borderTop: 0 }}>
              <summary>{clothingItemLabel(selectedProfileItem)} judgment prompt</summary>
              <p style={{ whiteSpace: 'pre-wrap', fontSize: '0.9rem', lineHeight: 1.65, margin: '0.75rem 0 0' }}>
                {selectedItemProfile?.taste_prompt ?? profile?.taste_prompt}
              </p>
            </details>
            {selectedItemProfile?.cross_item_influence?.length ? (
              <div className="tag-row" style={{ marginTop: '1rem' }}>
                {selectedItemProfile.cross_item_influence.map((item) => (
                  <span className="tag subtle" key={item}>{item}</span>
                ))}
              </div>
            ) : null}
          </article>
        )}
        {judgmentPromptQuery.data && (
          <article className="card" style={{ marginTop: '1rem' }}>
            <details className="details-block" style={{ marginTop: 0, paddingTop: 0, borderTop: 0 }}>
              <summary>Full VLM judgment prompt (v{judgmentPromptQuery.data.profile_version})</summary>
              <p className="muted" style={{ fontSize: '0.82rem', margin: '0.75rem 0 0' }}>
                {judgmentPromptQuery.data.character_count.toLocaleString()} characters ·{' '}
                {judgmentPromptQuery.data.token_count.toLocaleString()} tokens ({judgmentPromptQuery.data.tokenizer})
              </p>
              <pre style={{ whiteSpace: 'pre-wrap', fontSize: '0.82rem', lineHeight: 1.55, margin: '0.75rem 0 0', maxHeight: '40rem', overflow: 'auto', background: 'var(--surface-2)', border: '1px solid var(--border)', padding: '0.75rem', borderRadius: '6px' }}>
                {judgmentPromptQuery.data.prompt}
              </pre>
            </details>
          </article>
        )}
        {selectedItemProfile?.summary && (
          <article className="card" style={{ marginTop: '1rem' }}>
            <ProfileHeading>{clothingItemLabel(selectedProfileItem)} summary</ProfileHeading>
            <p style={{ margin: 0, fontSize: '0.9rem', lineHeight: 1.6 }}>{selectedItemProfile.summary}</p>
          </article>
        )}
        {selectedRubric.length > 0 && (
          <article className="card" style={{ marginTop: '1rem' }}>
            <ProfileHeading>Scoring rubric</ProfileHeading>
            <dl className="rubric-list">
              {selectedRubric.map(([band, description]) => (
                <div className="rubric-list__row" key={band}>
                  <dt>{band}</dt>
                  <dd>{description}</dd>
                </div>
              ))}
            </dl>
          </article>
        )}
        <div className="card-grid two-up" style={{ marginTop: '1rem' }}>
          <article className="card">
            <ProfileHeading>Likes</ProfileHeading>
            <ProfileBullets items={selectedLikes} />
          </article>
          <article className="card">
            <ProfileHeading>Dislikes / penalties</ProfileHeading>
            <ProfileBullets items={selectedDislikes} />
          </article>
        </div>
        <div className="card-grid two-up" style={{ marginTop: '1rem' }}>
          <article className="card">
            <ProfileHeading>Instant alert examples</ProfileHeading>
            <ProfileBullets items={selectedAlertExamples} />
          </article>
          <article className="card">
            <ProfileHeading>Instant reject examples</ProfileHeading>
            <ProfileBullets items={selectedRejectExamples} />
          </article>
        </div>
      </TasteSection>

      {/* Upload modal */}
      {uploadOpen && (
        <UploadModal
          onClose={() => setUploadOpen(false)}
          onUploadFile={(file, note) => uploadMutation.mutateAsync({ file, note, clothingItem: uploadTargetItem })}
          clothingItem={uploadTargetItem}
        />
      )}
      {offerOpen && (
        <AddOfferModal
          onClose={() => setOfferOpen(false)}
          onSubmit={(payload) => addOfferMutation.mutateAsync(payload)}
        />
      )}
    </section>
  );
}
