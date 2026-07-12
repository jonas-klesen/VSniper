# Plan: remaining UX fixes

## Context

A prior review enumerated UX gaps in the web app. Several have been fixed ("ux fixes part 1"); this plan covers the remaining six. The common thread: backend data is already being fetched but not surfaced, mutation results are lost on navigation, and forms/uploads silently discard work. Each fix makes existing state visible/durable rather than adding new domain logic.

Confirmed decisions:
- **Run summaries**: persist all three counts (fetched/judged/alerted) via an Alembic migration.
- **Multi-image viewer**: inline main image + clickable thumbnail strip (no lightbox, no deps).
- **Scan-all**: triggers **live** runs across enabled searches, sequential to respect the DB run-claim lock.

---

## 1. Scan-all button + persistent run summaries  (med/high)

**Backend — persist fetched/judged/alerted**

- `backend/src/vsniper/db/models.py` (`Search`): add `last_fetched_count: int` and `last_judged_count: int` columns (default 0), alongside existing `last_found_count`.
- New Alembic migration: `uv run --project backend --extra dev alembic revision --autogenerate -m "search run counts"`, review, keep.
- `backend/src/vsniper/services/search_service.py::_persist_run`: compute `judged_count` = number of candidates whose `stages.get(candidate_id) == "vlm_judged"` (i.e. not `regex_rejected`); on `mode == "live"` set `search.last_fetched_count = len(raw_candidates)`, `search.last_judged_count = judged_count` next to the existing `last_found_count = alert_count`.
- `backend/src/vsniper/domain/contracts.py` (`SearchRecord`): add `last_fetched_count: int` and `last_judged_count: int`.
- `backend/src/vsniper/services/_mapping.py::search_to_record` (~line 114): map the two new fields.

**Backend — scan-all endpoint**

- `backend/src/vsniper/api/routes/searches.py`: add `POST /api/searches/run-all` that iterates enabled searches and runs each live via `state.searches`. Run **sequentially** (the existing `claim_for_run` DB lock + the worker already serialize runs; do not add an in-process lock per CLAUDE.md). Return a small summary list (`list[SearchTestRunResult]`) or an aggregate. Reuse the existing per-search live-run service path (`_run(..., mode="live")`); add a thin `run_all_enabled()` method on `SearchService` so the route stays thin.

**Frontend**

- `web/src/types.ts` (`SearchRecord`): add `last_fetched_count: number`, `last_judged_count: number`.
- `web/src/lib/api.ts`: add `runAllSearches: () => request(...'/api/searches/run-all', { method: 'POST' })`.
- `web/src/pages/SearchesPage.tsx`: add a "Run all" button in the page header; mutation calls `runAllSearches`, `onSuccess` calls existing `invalidateSearchRunData()`. Disable while pending.
- `web/src/components/searches/SearchBuilder.tsx` (`SearchCard`, ~line 627): render the persisted counts — e.g. `Fetched {search.last_fetched_count} · Judged {search.last_judged_count} · Alerts {search.last_found_count}` next to `Last run`. These now come from `SearchRecord` (persisted), so they survive navigation; the mutation-local `runResult` summary line can remain as an immediate post-run toast but is no longer the source of truth.

---

## 2. Delivery error + source-search name + retry  (medium)

**Backend — expose source search name**

- `backend/src/vsniper/domain/contracts.py` (`CandidateRecord`): add `source_search_name: str | None`.
- `backend/src/vsniper/services/_mapping.py::candidate_to_contract` (~line 137): set `source_search_name` from `model.search.name` if the relationship is loaded (verify `Candidate` has a `search` relationship; if not, fall back to a name lookup in `CandidateService` listing, or eager-load). Cheapest path: include the search name when building the candidate list in `candidate_service.py`.

**Backend — manual retry endpoint**

- `backend/src/vsniper/services/telegram_service.py`: add `retry_delivery(candidate_id)` that finds the latest `AlertDeliveryState` for the candidate and, if `status == "failed"`, resets it to `pending` (attempt_count handling per existing `_record_failure` logic) so the worker picks it up next cycle. Reuse existing eligibility/backoff constants.
- `backend/src/vsniper/api/routes/candidates.py` (or `telegram.py`): add `POST /api/candidates/{candidate_id}/retry-delivery` delegating to `state.telegram.retry_delivery`.

**Frontend**

- `web/src/types.ts`: add `source_search_name` to `CandidateRecord`.
- `web/src/lib/api.ts`: add `retryDelivery: (candidateId) => request(...'/retry-delivery', { method: 'POST' })`.
- `web/src/pages/CandidatesPage.tsx` (~line 287-289): in the detail list render `Source: {candidate.source_search_name ?? candidate.source_search_id}`; extend the Delivery line to append `— {candidate.telegram_delivery_last_error}` when status is `failed` and an error exists; when status is `failed`, show a small "Retry delivery" button wired to a mutation that invalidates `queryKeys.candidates`.

---

## 3. Multi-image viewer for candidates  (medium)

- `web/src/pages/CandidatesPage.tsx` (~line 275): replace the single `<img>` with a small inline gallery. Per-card local state `const [activeImage, setActiveImage] = useState(0)` — extract a `CandidateGallery` component to keep hook usage clean (the cards are rendered in a `.map`, so the gallery must be its own component to legally hold state).
  - Main image: `image_urls[activeImage]`.
  - Below it, a thumbnail row mapping `image_urls`, each a small `<img>` button that calls `setActiveImage(i)`; highlight the active one. Only render the strip when `image_urls.length > 1`.
- `web/src/styles.css`: add `.candidate-thumbs` (flex row, gap, overflow-x auto) and `.candidate-thumb` / `.candidate-thumb.active` styles, reusing the existing `.candidate-image` look for the main image.

---

## 4. Upload modal: append / bucket header / non-image message  (medium)

All in `web/src/pages/MyTastePage.tsx` (`UploadModal`, lines ~26-160):

- **Append**: change `absorb` (line ~52) to merge: `setFiles((prev) => [...prev, ...accepted])` instead of replacing. Consider de-duping by name+size. (Optional: add per-file remove buttons in the preview list since selection now accumulates.)
- **Non-image message**: in `absorb`, compute `accepted` vs `rejected = Array.from(fileList).length - accepted.length`; if `rejected > 0` call `setError(\`${rejected} non-image file(s) were skipped.\`)` instead of unconditionally clearing the error.
- **Bucket in header**: add `clothingItem: ClothingItem` to `UploadModal` props; pass `uploadTargetItem` from the parent (line ~1041); header becomes `Add wardrobe photos · {clothingItemLabel(clothingItem)}`. `clothingItemLabel` already exists and is imported on the page.

---

## 5. Unsaved-changes guard (settings form + taste note)  (low/med)

React Router is v6.30.1 → `useBlocker` is available.

- Create a small reusable hook `web/src/lib/useUnsavedGuard.ts`: `useUnsavedGuard(isDirty: boolean)` that (a) registers a `beforeunload` handler when dirty (tab close/refresh) and (b) uses `useBlocker(({currentLocation, nextLocation}) => isDirty && currentLocation.pathname !== nextLocation.pathname)`, showing a `window.confirm` and `blocker.proceed()/reset()` on the decision.
- `web/src/pages/SettingsPage.tsx`: the dirty signal already exists as `formDirty` (useRef). `useBlocker` needs a reactive boolean, not a ref — promote dirtiness to state (`const [dirty, setDirty] = useState(false)`, set true on edit at line ~212, reset to false in the save `onSuccess`), then `useUnsavedGuard(dirty)`.
- `web/src/pages/MyTastePage.tsx`: same change for the taste note — replace/augment `manualNoteDirty` ref with reactive state (or derive `manualNote !== savedNote`) and call `useUnsavedGuard(...)`.

> Note: `useBlocker` requires a data router. The app uses `createBrowserRouter` (`web/src/app/router.tsx`), so `useBlocker` is supported.

---

## 6. Costs page: invalidation + prefilter stage clarification  (low/med)

- **Invalidation**: after a recompute (MyTastePage) and after scan runs (SearchesPage `invalidateSearchRunData`), also `queryClient.invalidateQueries({ queryKey: queryKeys.costs })` so spend refreshes. Add `queryKeys.costs` to those invalidation sites.
- **Prefilter stage**: confirmed in backend (`candidate_service.py::get_ai_cost_stats`) that the regex prefilter performs **no LLM call and records no cost** — there is genuinely no prefilter spend to show. Resolution is documentation, not a new stage: add a one-line note on `web/src/pages/CostsPage.tsx` (near the Judge/Learning rows) e.g. "Regex prefilter runs before judging and incurs no AI cost." No backend change. (The CLAUDE.md docs already state the prefilter records no AI usage, so docs and code agree — the "missing stage" is expected.)

---

## Verification

1. **Backend**: `uv run --project backend --extra dev alembic upgrade head` then `uv run --project backend --extra dev pytest backend/tests`; `ruff check` + `mypy` on `backend/src`. Add/adjust a mapping test if one covers `search_to_record`/`candidate_to_contract`.
2. **Run-all + counts**: start API + web (`docker compose up` or the dev commands in CLAUDE.md). On Searches page, click "Run all", confirm each card shows fetched/judged/alerts and that the values persist after navigating away and back (and after API restart).
3. **Candidates**: on a candidate with a failed delivery, confirm the error reason + source search name render and the "Retry delivery" button flips status to pending; confirm multi-image candidates show a working thumbnail strip.
4. **Upload modal**: open from a specific bucket — header shows the bucket; selecting files twice accumulates; dropping a non-image shows the skip message.
5. **Unsaved guard**: edit settings/taste note, attempt nav + refresh → confirm prompt; save then nav → no prompt.
6. **Costs**: trigger a recompute/scan, confirm the costs figures refresh without a manual reload; confirm the prefilter note renders.
