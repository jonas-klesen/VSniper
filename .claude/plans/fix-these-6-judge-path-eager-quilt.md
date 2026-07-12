# Fix judge-path robustness + DB-transaction I/O (issues 6, 7, 8)

## Context

Three reliability defects in the scan/judge/feedback paths, all of which fail or
double-spend a scan under conditions that are routine with a local llama.cpp judge
or during an OpenAI outage:

- **#6 — judge error boundary leaks non-`OpenAIIntegrationError` exceptions.** A local
  model returning `score: 0` (llama.cpp doesn't enforce schema `min`/`max`) raises a
  pydantic `ValidationError` at `_GridJudgmentPayload.model_validate`; an `"input_tokens": null`
  in the usage block raises `TypeError` in `_emit_usage`. Neither is an `OpenAIIntegrationError`,
  so both escape the split-and-retry recovery in `_judge_image_batch` and fail the whole scan —
  discarding batch results that were already billed.
- **#7 — nested retry amplification.** `judge_candidate_grid` wraps `_request_and_parse`
  (outer `retry_transient`, 3×) which calls `_create_response` (inner `retry_transient`, 3×),
  and `_judge_image_batch` then splits a failed 9-tile grid into 9 single-item re-judges. During
  an outage this is up to ~90 HTTP attempts per grid, easily exceeding the 600s stale-claim window
  so a second worker reclaims and double-spends. Also, truncation retries resend identically and
  re-truncate.
- **#8 — network I/O inside open write transactions.** Four code paths hold a SQLite write
  txn across httpx calls. Under WAL these hit `SQLITE_BUSY_SNAPSHOT` (the busy-timeout does not
  apply to snapshot conflicts) when the other process commits mid-call → sporadic 500s / failed
  runs. The codebase already has a three-phase pattern (read txn → network, no lock → write txn);
  apply it.

Intended outcome: a bad local-model judgment or a transient outage degrades to a discarded/retried
batch (scan continues), retry counts stay bounded, and no network call runs under a held write lock.

---

## #6 — Tighten the judge error boundary

File: `backend/src/vsniper/integrations/openai/client.py`

1. **`_emit_usage` (lines 669–673):** an explicit `"input_tokens": null` makes
   `usage.get("input_tokens", <default>)` return `None` → `int(None)` raises. Replace the
   `.get(a, b)` chains with `or`-fallbacks so any null/missing field coerces to 0:
   ```python
   input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
   output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
   ```
   (Keep the existing `details` / `cached_tokens` handling.)

2. **`judge_candidate_grid` `_GridJudgmentPayload.model_validate` (line 1445):** wrap the
   validate (and `_normalize_grid_keys`) in `try/except ValidationError` and re-raise as a
   **non-retryable** `OpenAIIntegrationError`, so it lands in `_judge_image_batch`'s
   `except OpenAIIntegrationError` boundary (search_service.py:660) and follows the existing
   split-then-mark-failed recovery instead of escaping the scan:
   ```python
   try:
       parsed = _GridJudgmentPayload.model_validate(_normalize_grid_keys(parsed_payload))
   except ValidationError as exc:
       raise OpenAIIntegrationError(f"Grid judgment failed schema validation: {exc}") from exc
   ```
   `ValidationError` is already imported (client.py:18).

---

## #7 — Bound nested retries

1. **`retry_transient` (`backend/src/vsniper/integrations/_retry.py:17`):** when attempts are
   exhausted, clear the retryable flag on the exception before re-raising, so an *outer*
   `retry_transient` treats it as terminal instead of re-amplifying. In the
   `attempt >= _MAX_ATTEMPTS - 1` branch (line 25), before `raise`:
   ```python
   if getattr(exc, "retryable", False):
       exc.retryable = False  # exhausted here; don't let an outer retry_transient re-amplify
   raise
   ```
   This is safe for every call site: the only nesting is openai client inner (line 783) within
   outer (line 1444); `SearchService._run`'s `retry_transient` (search_service.py:725) and the
   Vinted client's separate internal retry loop are unaffected (the Vinted loop reads its own
   exception's `.retryable`, not via `retry_transient`). Once retries are spent the error is
   terminal, so clearing the flag is also semantically correct in the non-nested cases.

2. **Bump `max_output_tokens` on truncation retry (`judge_candidate_grid`, client.py:1415–1436):**
   rename the budget to a closure-mutable local and grow it on each truncation retry instead of
   resending identically:
   ```python
   grid_max_output_tokens = 400 * len(expected_positions) + 800

   def _request_and_parse() -> Any:
       nonlocal grid_max_output_tokens
       payload = self._create_response(..., max_output_tokens=grid_max_output_tokens, ...)
       ...
       if payload.get("status") == "incomplete" and reason == "max_output_tokens":
           grid_max_output_tokens = min(int(grid_max_output_tokens * 1.5), 8000)
           raise _retryable_error(...)
   ```
   Cap chosen to stay well under the model context; truncation only applies to the non-local
   (OpenAI) branch, unchanged.

---

## #8 — Move network I/O out of held write transactions

Apply the existing three-phase pattern (canonical examples: `SearchService._run`
search_service.py:687, `CandidateService.apply_feedback` candidate_service.py:225;
`session_scope()` already uses `expire_on_commit=False`, so Phase-1 reads stay valid detached).

### (a) Feedback image download — the worst

Files: `candidate_service.py`, `taste_service.py`

`apply_feedback` is already three-phase but only moved the VLM observation out of the write txn;
`upsert_candidate_feedback_sample` → `_cache_image_urls` (taste_service.py:296, up to 6×20s
downloads) still runs in Phase 3. All feedback (web + Telegram) funnels through `apply_feedback`,
so fixing it there is complete.

- The cache filename prefix is currently `sample.id`, unknown in Phase 2. Decouple it: key the
  cached files on `candidate.id` (stable, known in Phase 1/2). Add
  `TasteService.precache_feedback_images(self, *, candidate_id, image_urls) -> list[str]` that
  just calls `self._cache_image_urls(candidate_id, image_urls or [])` (no DB access).
- In `taste_service._cache_image_urls`' two call sites (lines 197, 296) and the upsert, use
  `candidate.id` as the prefix. Add `precached_image_paths: list[str] | None = None` to
  `upsert_candidate_feedback_sample`; replace line 296 with:
  ```python
  sample.cached_image_paths = (
      sample.cached_image_paths or precached_image_paths
      or self._cache_image_urls(candidate.id, sample.image_urls)
  )
  ```
  (trailing call is a no-network fallback for empty url lists / other callers).
- Add the same `precached_image_paths` kwarg to `record_feedback_in_session`
  (candidate_service.py:271), forwarded at line 295.
- In `apply_feedback`: Phase 1 also capture `candidate_image_urls = candidate_model.image_urls or []`;
  Phase 2 (after the observation) call `self.preferences.precache_feedback_images(...)`; Phase 3
  pass the result through `record_feedback_in_session`.
- Race: if another process populates `cached_image_paths` between phases, Phase 3 keeps the
  existing value (our precomputed paths are discarded — bounded, harmless), mirroring the existing
  observation guard. The `_replace_cached_observation` block (taste_service.py:297–322) keys on
  `sample.id` for internal observation-cache ids, NOT the downloaded filenames — leave it.

### (b) Session-health refresh

File: `search_service.py`

Split `_refresh_session_health` (line 895) into read-only staleness check + lock-free fetch:
```python
def _session_health_needs_refresh(self, model, *, region, force=False) -> tuple[SessionHealth, bool]:
    current = self._coerce_session_health(model.session_health, region=model.vinted_region)
    now = datetime.now(UTC)
    return current, force or self._session_health_is_stale(health=current, region=region, now=now)

def _fetch_session_health_json(self, *, region, force=False) -> dict:
    return self.vinted_client.get_session_health(region=region, force=force).model_dump(mode="json")
```
- `_run` (line 717): Phase 1 computes `needs`; Phase 2 fetches `sh_json` if `needs`; thread
  `sh_json` into `_persist_run` (add a kwarg, set `model.session_health` there if not None).
- `get_app_settings` (line 906): three-phase — Phase 1 read + client `set_*` + needs-check; Phase 2
  fetch if needed; Phase 3 re-load, write `session_health`, flush, return contract.
- `update_app_settings` (line 915, `force=True`): fetch depends on the *new* cookie/region from
  `payload` (and current DB value when payload field is `None` = unchanged). Phase 1 read txn for
  current cookie/token to resolve effective values; Phase 2 client `set_*` + `_fetch_session_health_json(region=payload.vinted_region, force=True)`; Phase 3 write txn applies all field
  assignments (lines 917–952) and `model.session_health = sh_json` (replacing 953–955).
- Stale-write race is benign (advisory health + TTL cache); keep the simple unconditional write.

### (c) Telegram token-expiry warning

File: `telegram_service.py`, `check_refresh_token_expiry` (line 505)

`send_message` (line 540) runs inside the write txn (line 515). Three-phase:
- Phase 1 (read): load model, derive `config`, `self._apply_bot_token(config)`, capture
  `already_sent = model.refresh_token_expiry_warning_sent_for == expiry_key`; early-return on
  already-sent / unconfigured bot token / chat id.
- Phase 2 (no lock): build `text`, `try: send_message(...) except Exception: log + return`.
- Phase 3 (write): re-load model; idempotent write
  `if model and model.refresh_token_expiry_warning_sent_for != expiry_key: model... = expiry_key`.
- Single-worker job, so concurrent duplicate sends are effectively impossible; the Phase-3
  re-check keeps the write idempotent.

---

## Verification

1. Lint + type-check: `uv run --project backend --extra dev ruff check backend/src && uv run --project backend --extra dev mypy backend/src`
2. Tests: `uv run --project backend --extra dev pytest backend/tests` — focus on
   `test_judge_pipeline.py`, `test_candidate_service.py`, `test_scoring_learning.py`,
   `test_telegram_and_deliveries.py`, `test_search_claim.py`.
3. Add/extend tests:
   - judge: a local-provider payload with `score: 0` and one with `"usage": {"input_tokens": null}`
     → assert the batch yields `failed` traces (split/mark-failed), not a raised exception, and the
     scan completes.
   - retry: assert an exhausted inner `retry_transient` clears `.retryable` so an outer wrapper does
     not re-attempt (count HTTP calls via a stub client); assert truncation retry sends a larger
     `max_output_tokens`.
4. Targeted manual check for #8: run a feedback apply and a `get_app_settings`/`update_app_settings`
   while the worker is scanning; confirm no `SQLITE_BUSY`/snapshot 500s (concurrency is the failure
   trigger, so exercise both processes).
