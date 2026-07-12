# Fix still-applicable frontend/UX improvements

## Context

A prior review listed five UX improvements. Re-checking each against the current
code shows some are now stale (already implemented) and some still apply:

- **#4 Render recompute_state — already done.** `MyTastePage` already polls every
  3s while running (`refetchInterval` keyed on `recompute_state.status`), shows the
  running message, last error, and last cost. **Excluded from this plan.**
- The "freeze every button on the page" part of **#1** is also already fixed —
  buttons disable *per-candidate* via `feedbackMutation.variables?.candidateId`.

The four items below are confirmed still-applicable and were selected for this plan.
For **#5**, scope is "decision + feedback + sort" (not the full bucket/source/price set).

---

## #2 — Add taste offer by Vinted URL (frontend-only, small)

The endpoint `POST /api/taste/offers/from-url` and `api.addTasteOffer` (web/src/lib/api.ts:118)
both exist but are never called. `add_offer` (taste_service.py:182) simply *stores* the
given fields (it does not scrape the URL), so the form only needs: `vinted_url`, `kind`
(like/dislike), `clothing_item`, optional `note`.

**File: `web/src/pages/MyTastePage.tsx`**
- Add an `AddOfferModal` component modeled on the existing `UploadModal`
  (MyTastePage.tsx:26) — same modal portal + `useModalDismiss` pattern. Fields: URL
  input, kind toggle (Liked/Disliked), `clothing_item` `<select>` (reuse
  `clothingItems` / `clothingItemLabel` from `lib/clothingItems`), optional note.
- Add an `addOfferMutation` next to the existing taste mutations (uploadMutation etc.,
  ~line 655) calling `api.addTasteOffer`; on success `invalidateQueries(queryKeys.taste)`
  and close the modal.
- Add an "Add by URL" button in the "Offer feedback" `TasteSection` header
  (~MyTastePage.tsx:834) that opens the modal.

---

## #1 — Optimistic per-candidate vote (frontend-only, small)

**File: `web/src/pages/CandidatesPage.tsx`** (feedbackMutation at line 121)
- Add `onMutate` to `feedbackMutation`: `await queryClient.cancelQueries({ queryKey: queryKeys.candidates })`,
  snapshot current candidates caches, then `queryClient.setQueriesData({ queryKey: queryKeys.candidates }, …)`
  to set the voted candidate's `feedback` (`like`/`dislike`) and `feedback_comment`
  optimistically. Return the snapshot as context.
- Add `onError(_e,_v,ctx)` to roll the snapshot back.
- Replace the `onSuccess` invalidations with `onSettled` invalidating
  `queryKeys.candidates`, `queryKeys.taste`, and `queryKeys.stats` (taste/stats stay so
  dirty-counts and dashboard tallies refresh).
- The per-candidate `disabled` guard (line 96) already stays correct.

---

## #3 — Dashboard as health panel (frontend + one small backend add)

**Backend — add failed-deliveries count:**
- `backend/src/vsniper/domain/contracts.py`: add `failed_deliveries: int = 0` to
  `DashboardStats` (next to `pending_deliveries`, line ~404).
- `backend/src/vsniper/services/candidate_service.py` `get_dashboard_stats` (line 294):
  add a count query `select(func.count()).where(AlertDeliveryState.status == "failed")`
  and pass it into the `DashboardStats(...)`.
- `web/src/types.ts`: add `failed_deliveries: number` to the `DashboardStats` type.

**Frontend — `web/src/pages/DashboardPage.tsx`:**
- Add a `failed_deliveries` stat card (style it as a warning when > 0).
- **Cookie/refresh expiry:** import `decodeJwtExpiry`, `decodeRefreshTokenExpiry`,
  `formatCookieExpiry` from `lib/jwt` (already used on SettingsPage). `settings` already
  carries raw `vinted_cookie` / `vinted_refresh_token`; render access- and refresh-token
  expiry lines in the "Session health" column, red when expired.
- **Webhook status:** add a query using the existing `api.getTelegramWebhookStatus`
  (api.ts:201) — only `enabled` when `settings.telegram_configured`. Show a pill:
  registered / not-registered / URL-mismatch from `is_registered` +
  `matches_configured_url`.
- **Recompute-due banner:** add a query using `api.getTaste` (api.ts:105). If
  `dirty_counts.new_or_changed_samples > 0 || dirty_counts.manual_note_changed` and
  `recompute_state.status !== 'running'`, render a banner above the cards with a
  React-Router `<Link to="/taste">` to recompute.
- **Linkable stat cards:** wrap the candidate-related cards (Candidates today, Likes,
  Dislikes, Avg alert score) in `<Link>` to `/candidates` with query strings consumed by
  #5 (e.g. `/candidates?feedback=like`, `/candidates?decision=alert`).

---

## #5 — Candidate filtering/sorting: decision + feedback + sort (backend + frontend)

**Backend — `candidate_service.py` `page()` (line 156) + `routes/candidates.py` (line 9):**
- Add query params `decision: str | None`, `feedback: str | None`,
  `sort: str = "newest"` to both the route and `page()`.
- In `page()`, apply `.where(Candidate.decision == decision)` and
  `.where(Candidate.feedback == feedback)` when provided (alongside the existing `stage`).
- Replace the fixed `order_by(Candidate.created_at.desc())` with a mapping:
  `newest`→`created_at.desc()`, `oldest`→`created_at.asc()`, `price_asc`/`price_desc`→
  the price column, `score_desc`/`score_asc`→`final_score` (confirm column names against
  `candidate_to_contract` / the dashboard-stats queries which already use
  `Candidate.final_score`, `Candidate.decision`, `Candidate.feedback`).
- `total` can no longer be read from `stage_counts` once decision/feedback filters apply:
  compute `total` with a `select(func.count())` carrying the same where-clauses. Keep the
  existing per-stage `stage_counts` (unfiltered) for the stage buttons.

**Frontend — `web/src/lib/api.ts` `getCandidates` (line 145):**
- Extend params with `decision?`, `feedback?`, `sort?` and append them to the
  `URLSearchParams` when set.

**Frontend — `web/src/lib/queryKeys.ts`:**
- Change `candidatesPage` to take all filter dimensions (stage, decision, feedback, sort,
  offset, limit) so each combination caches separately.

**Frontend — `web/src/pages/CandidatesPage.tsx`:**
- Add `decision` filter (dropdown: all/alert/review/discard), `feedback` filter
  (dropdown: all/like/dislike/unknown), and `sort` dropdown
  (Newest/Oldest/Price ↑/Price ↓/Score ↓/Score ↑) beside the existing stage buttons.
- Seed initial filter state from the URL via React-Router `useSearchParams` (so the
  Dashboard links in #3 land pre-filtered); keep URL in sync on change.
- Reset `page` to 0 on any filter/sort change (extend the existing `selectStage` pattern).
- Pass the new values into `api.getCandidates` and the query key.

---

## Verification

1. **Backend tests + types:** `uv run --project backend --extra dev pytest backend/tests`,
   `uv run --project backend --extra dev mypy backend/src`,
   `uv run --project backend --extra dev ruff check backend/src`.
2. **Web build:** `cd web && npm run build` (tsc + vite) — confirms contract/type sync.
3. **Run full stack** (`docker compose up --build`, or API + worker + `npm run dev`) and
   manually check:
   - Candidates: toggle decision/feedback/sort, paginate; URL updates; results match.
   - Dashboard: failed-deliveries card; cookie/refresh expiry lines; webhook pill;
     recompute-due banner appears after adding a sample then disappears after recompute;
     stat-card links land on pre-filtered Candidates.
   - MyTaste: "Add by URL" creates an offer (appears in Liked/Disliked grid) and marks
     the profile dirty.
   - Candidates vote: Like/Dislike flips instantly (optimistic), persists after refetch,
     and rolls back if the request fails.
