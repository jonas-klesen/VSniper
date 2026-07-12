# Fix remaining backend hardening issues (#1, #3, #4)

## Context

A prior review flagged four backend issue classes. Re-investigating the current code shows **two are already fully resolved**:

- **#2 Two-process state sync** — the worker re-reads + re-applies the Vinted cookie/refresh-token from the DB at the top of every scan (`search_service._run`), `validate_cookie` snapshots/restores instead of mutating, and token refresh is serialized behind a `threading.Lock`. No work needed.
- **#1 Datetimes (the bug class)** — all columns are `DateTime(timezone=True)`, all creation uses `datetime.now(UTC)`, and a `_as_aware()` normalize-on-read helper already exists and is used in the comparison hot paths. No correctness work needed — only a small dedup (below).

Two issues still apply and are the substance of this plan:

- **#3 Transaction shape** — the delivery-queue path was correctly refactored into a three-phase (read → network → write) pattern, but the **feedback path still holds a DB write transaction open across a VLM call**. `record_feedback_in_session` → `_ensure_candidate_observation` downloads a candidate image and runs `describe_candidate_image` (network + VLM, potentially many seconds) while the SQLite write lock is held. This affects **both** the Telegram webhook (`_record_feedback_for_delivery`) and the web-UI route (`record_feedback`).
- **#4 Unbounded growth** — nothing prunes `Candidate`, `AlertDeliveryState`, or `AiUsageEvent`; `_pending_deliveries` loads *all* pending/processing rows into memory before filtering in Python. Outcome: a periodic prune job plus a SQL-bounded delivery queue. **Per the user, retention limits should be large.**

Plus the agreed minor cleanup:

- **#1 dedup** — consolidate the two duplicated `_as_aware()` helpers into one shared utility.

## Issue #3 — Move the VLM observation out of the feedback write transaction

The image download + `describe_candidate_image` VLM call must happen **outside** any DB session, mirroring the scan path's three-phase shape (`search_service._run` lines ~632-695: read → I/O → write).

### Files
- `backend/src/vsniper/services/candidate_service.py`
- `backend/src/vsniper/services/telegram_service.py`

### Plan
1. **Split `_ensure_candidate_observation` (candidate_service.py:78-116) into pure-network + pure-apply halves:**
   - `_compute_candidate_observation(candidate_snapshot) -> dict | None` — does the image download (`_load_candidate_image_bytes`) and `describe_candidate_image` call, returns the `observation.model_dump(mode="json")` dict (or `None` on failure/already-present/no-image). **No session touched.** It needs a few fields (image_urls, title, brand, size, normalized_listing, clothing_item) plus the settings snapshot — gather these in a short read txn first, or pass a small dataclass/snapshot captured by the caller.
   - The settings it reads (`_get_settings_state`) currently require an attached session; capture the needed settings fields in the read phase instead.
2. **Add a three-phase orchestrator for feedback** (new method on `CandidateService`, e.g. `apply_feedback(candidate_id, verdict, comment, skip_if_unchanged)`):
   - **Phase 1 (short read txn):** load candidate; if `skip_if_unchanged` and feedback already matches, short-circuit; capture the snapshot needed for observation (only if `ai_observation` is empty) + settings.
   - **Phase 2 (no txn):** compute the observation dict via `_compute_candidate_observation`.
   - **Phase 3 (short write txn):** re-load candidate, set feedback/comment, set `ai_observation` from the precomputed dict, run `upsert_candidate_feedback_sample`, write the `LearningSnapshotState`. Return `(CandidateRecord, LearningSnapshot | None)`.
3. **Rewire callers:**
   - `record_feedback` (web UI route) → delegate to `apply_feedback`.
   - `record_feedback_in_session` — keep as the write-phase helper, but it must **no longer call `_ensure_candidate_observation`**; instead accept an optional precomputed observation dict to apply. `telegram_service._record_feedback_for_delivery` is refactored to the same three-phase shape: read delivery+candidate snapshot (short txn) → compute observation (no txn) → write feedback + `delivery.updated_at` (short txn).
4. Keep best-effort semantics: observation failures are logged and do not block the feedback write (current behavior at candidate_service.py:113-115).

### Watch out for
- `record_feedback_in_session` is shared by both feedback entrypoints — preserve its return tuple and the `skip_if_unchanged` early-return path (candidate_service.py:178-181).
- The Telegram webhook already defers `run()` to a FastAPI `BackgroundTask` (telegram_service.py:883-889), so latency isn't the concern — **holding the SQLite write lock across the VLM call** is. Three-phasing fixes that regardless of background scheduling.

## Issue #4 — Retention prune job + bounded delivery queue

### New config (`backend/src/vsniper/core/config.py`, near line 52)
Add generous, env-overridable retention knobs (large defaults per user request):
- `candidate_retention_days: int = Field(default=365, alias="CANDIDATE_RETENTION_DAYS")`
- `delivery_retention_days: int = Field(default=365, alias="DELIVERY_RETENTION_DAYS")`
- `ai_usage_retention_days: int = Field(default=365, alias="AI_USAGE_RETENTION_DAYS")`
- `prune_every_cycles: int = Field(default=60, alias="PRUNE_EVERY_CYCLES")` — run the prune roughly once an hour at a 60s interval.

Document the same keys in `.env.example`.

### Prune service method
Add `CandidateService.prune_old_records(...)` (it already owns `Candidate` and reads `AiUsageEvent`/`AlertDeliveryState`):
- Delete `AiUsageEvent` rows with `called_at < now - ai_usage_retention_days`.
- Delete terminal `AlertDeliveryState` rows (`status in ("sent","failed")`) with `updated_at < now - delivery_retention_days`. **Never** delete `pending`/`processing` rows.
- Delete `Candidate` rows with `created_at < now - candidate_retention_days`. Because `AlertDeliveryState.candidate_id` FKs `candidates.id`, delete that candidate's delivery rows first (or only prune candidates with no surviving deliveries) to avoid FK violations.
- Use bulk `delete()` statements inside one short `session_scope()`; log counts deleted. Use `datetime.now(UTC)` for the cutoffs (consistent with existing aware columns + `_as_aware` reads).

### New worker job + scheduler wiring
- New `backend/src/vsniper/worker/jobs/prune_records.py` with `run_once()` calling `get_state().candidates.prune_old_records()` (mirrors `process_deliveries.py`).
- In `backend/src/vsniper/worker/scheduler.py`: add a module-level cycle counter; in `cycle()` (inside the `finally` block alongside `process_deliveries_once`/`check_cookie_expiry`), call the prune job every `prune_every_cycles` cycles, wrapped in try/except so a prune failure never kills the worker (match the existing `check_cookie_expiry` guard at lines 91-94).

### Bound the delivery queue (`telegram_service._pending_deliveries`, lines 280-290)
Currently `.all()` loads every pending/processing row, then filters eligibility in Python. Add `.limit(...)` to the query so memory stays bounded as the table grows. Caller `_claim_pending_deliveries` already caps claims at `limit` (default 25); fetch a modest multiple (e.g. `limit * 4`) to leave headroom for the Python eligibility filter (`_is_eligible` drops not-yet-due retries). Keep `order_by(created_at.asc())` so oldest are still served first.

### Optional index check
`AiUsageEvent.called_at` and `AlertDeliveryState.updated_at` are queried by the prune cutoffs. `Candidate.created_at` is already indexed (models.py:65). If `called_at`/`updated_at` lack indexes and pruning scans become slow, add indexes via an Alembic autogenerate migration. Defer unless needed — with large retention the prune runs infrequently.

## Issue #1 (minor) — Dedup `_as_aware`

Two copies exist: `services/_mapping.py:89-95` (handles `None`, canonical) and `services/telegram_service.py:259-265` (no `None` handling). Keep the `_mapping.py` version as the single source; import and use it in `telegram_service.py` (its `_is_eligible` at lines 275-278), and delete the local static method. Pure refactor, no behavior change.

## Verification

1. **Tests:** `uv run --project backend --extra dev pytest backend/tests`
   - Existing `test_vinted_client.py` concurrency test should still pass (untouched).
   - Add a test asserting `apply_feedback` records feedback even when the VLM observation call raises (best-effort), and that no session is open during the observation call (e.g. patch `describe_candidate_image` to assert the candidate is detached / no active write txn).
   - Add a `prune_old_records` test: seed old + recent rows across the three tables (and a pending delivery), run prune, assert old terminal/usage/candidate rows are gone and recent + pending rows survive.
   - Add a `_pending_deliveries` test asserting the SQL `LIMIT` caps the fetched rows.
2. **Lint/type:** `uv run --project backend --extra dev ruff check backend/src backend/tests` and `... mypy backend/src`.
3. **Manual smoke:**
   - Run a single worker cycle: `uv run --project backend python -m vsniper.worker.scheduler --once` — confirm it completes and (when the cycle counter hits the threshold) logs prune counts.
   - Exercise feedback via the web UI / `POST /api/candidates/{id}/feedback` and the Telegram webhook; confirm feedback + observation persist and no "database is locked" warnings appear under concurrent scan + feedback.

## Out of scope (already resolved)
- #2 Vinted two-process cookie sync — no change.
- #1 datetime correctness — no change beyond the helper dedup.
