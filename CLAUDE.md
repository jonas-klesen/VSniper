# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Clean-room rule

`fbm-sniper-community/` is a cloned upstream project kept **only** as human-readable reference material. It is not part of the runtime, is gitignored, and you must not import, copy, or transliterate code from it into `backend/`, `web/`, or anywhere else in the new tree. Concepts and patterns are fair game; lines of code are not.

## Common commands

Backend (run from repo root, `backend/` is a `uv`/pip editable install targeting Python 3.12):

- Install dev deps: `uv sync --project backend --extra dev` (lockfile at `backend/uv.lock`)
- Run API: `uv run --project backend uvicorn vsniper.api.main:app --reload --app-dir backend/src`
- Run worker loop: `uv run --project backend python -m vsniper.worker.scheduler` (add `--once` for a single cycle, `--interval N` to change cadence)
- Tests: `uv run --project backend --extra dev pytest backend/tests` — single test: `... pytest backend/tests/test_vinted_client.py::test_name`
- Lint / type-check: `uv run --project backend --extra dev ruff check backend/src backend/tests` and `uv run --project backend --extra dev mypy backend/src`

Web (run from `web/`):

- `npm run dev` — Vite dev server (port 5173, proxies `/api` to the backend; in Compose the target is `VITE_DEV_PROXY_TARGET`)
- `npm run build` — `tsc -b && vite build`
- `npm run preview` — preview the built bundle

Full stack: `docker compose up --build` brings up `api`, `worker`, and `web` against the mounted `./storage` and `.env`. Copy `.env.example` to `.env` before running.

## Prompt-iteration scripts (untracked)

`scripts/iter_prompts.py` and `scripts/iter_judge.py` exist to A/B prompt variants against the user's own reference photos in `pics_of_clothes_i_like/`. They hit the live OpenAI API using credentials from `.env`, so they cost a few cents per run. Their JSON / JPEG outputs are gitignored. Use them whenever you change a meta-prompt — run `current` and `revised` variants side-by-side and compare numerically before editing `backend/src/vsniper/integrations/openai/client.py`.

## Architecture

### Two-process backend, one shared state layer

The backend ships as **two processes** that share a single SQLite database under `storage/sqlite/vsniper.db`:

1. **API** (`vsniper.api.main:app`) — FastAPI app composed of routers in `vsniper/api/routes/` (health, stats, searches, taste, candidates, settings, telegram). Each route delegates to one of the four services on `AppState`.
2. **Worker** (`vsniper.worker.scheduler`) — a loop that, per cycle, (a) scans every enabled search in parallel via `worker/jobs/scan_search.py`, then (b) drains the Telegram alert delivery queue via `worker/jobs/process_deliveries.py`. The scheduler uses `state.searches.claim_for_run(...)` as a DB-backed lock so multiple workers can coexist; **do not** add an in-process lock alongside it.

Both processes call `vsniper.core.state.get_state()` which returns the same `AppState` instance defined in `vsniper/core/sqlite_state.py`. `AppState` is a **thin facade** (~200 lines) that owns shared resources (Settings, integration clients, DB lifecycle) and exposes four services:

- `state.taste` (also aliased as `state.preferences`) — `services/taste_service.py`: wardrobe sample CRUD, offer sample management, taste profile recompute (`recompute()` orchestrates describe_reference_images → build_taste_profile), `active_taste_profile()`, and `latest_labeled_anchors()` for judge calibration
- `state.candidates` — `services/candidate_service.py`: candidate listing, feedback recording (`record_feedback_in_session`), candidate-image observation capture on vote, dashboard stats, AI cost stats
- `state.telegram` — `services/telegram_service.py`: webhook registration/validation, outbound delivery queue/retry/processing, inbound callback handling, message edits
- `state.searches` — `services/search_service.py`: search CRUD, scan orchestration (`_run` → `_judge_candidates`), DB-backed run claim, session-health refresh, settings get/update (`get_app_settings`/`update_app_settings`)

Cross-service wiring (set up in `AppState.__init__`): `SearchService` calls into `TasteService` (for the active taste profile and labeled anchors during scan) and `TelegramService` (to queue deliveries); `TelegramService` calls into `CandidateService` (for `record_feedback_in_session` during webhook handling). Shared contract↔model converters and env-derived helpers live in `services/_mapping.py` as module-level functions.

Rule of thumb: business logic belongs in the relevant service. API routes and worker jobs stay thin — they pull `get_state()` and call a service method. If you find yourself reaching into SQLAlchemy models from a route or job, move it into the matching service instead. Don't add new methods directly to `AppState`; the facade should keep shrinking, not growing.

### Domain contracts are the wire format

`vsniper/domain/contracts.py` defines Pydantic models (`SearchRecord`, `CandidateRecord`, `TasteProfile`, `ScoreTrace`, `TelegramWebhookResult`, etc.) used as both the FastAPI response schemas and the internal data shape. The web client mirrors these in `web/src/types.ts` — when you change a contract, update both sides. The SQLAlchemy models in `vsniper/db/models.py` are an **internal** persistence shape distinct from the contracts; `AppState` converts between them.

### Clothing item taste buckets

Taste is item-specific. The fixed `ClothingItem` buckets are:

- `schuhe` / Schuhe — shoes and sneakers
- `hosen` / Hosen — trousers, jeans, cargos, shorts, and other legwear
- `obenrum_warm` / Obenrum Warm — warm upper-body pieces such as T-shirts and short-sleeve shirts
- `obenrum_mittel` / Obenrum Mittel — mid-layer tops such as longsleeves and light pullovers
- `obenrum_kalt` / Obenrum Kalt — cold-weather upper-body pieces such as heavy pullovers and jackets
- `kopf` / Kopf — headwear, especially funny/weird baseball caps

Every wardrobe sample, generated search draft, saved search, candidate, reference observation, and feedback example carries `clothing_item`. `TasteProfile` is still the top-level recompute artifact, but it contains one `ClothingItemTasteProfile` per bucket. Each item profile has its own prompt, rubric, transparency labels, and exactly one generated Vinted search draft.

Cross-item leakage is intentional: an item profile should be strongest on evidence from its own bucket, but `cross_item_influence` should carry broader wardrobe taste from other buckets (palette, era, texture, humor, subculture, silhouette principles) without forcing exact garment matches. At scan time, `SearchService` selects the item profile for `Search.clothing_item`; feedback inherits the candidate/search bucket so the next recompute can separate direct evidence from cross-item influence.

### VLM scoring pipeline

Every scan sends every fetched candidate to VLM judging so each alert/discard is explainable:

**VLM grid judging** (`SearchService._judge_candidates` → `OpenAITasteClient.judge_candidate_grid`) — all fetched candidates with usable images are assembled into 1-, 4-, or 9-image contact-sheet batches and sent to the configured judge model. The number of concurrent grid requests is controlled by `vlm_judge_parallel_requests`. The model returns a structured 1–10 score, explanation, labels, and concerns per position. `build_judgment_trace` converts each score to a `ScoreTrace`: score ≥ 7 → `alert`, ≥ 5 → `review`, < 5 → `discard`. If image download fails or the model returns null for a position, `build_failed_judgment_trace` produces a `discard` trace tagged `failed`.

The judge model can be OpenAI-backed or local. `ai_judge_provider == "local"` means judging only: `OpenAITasteClient.judge_candidate_grid()` must call `POST {LOCAL_VLM_BASE_URL}/responses`, never `/chat/completions`. Local calls use `local_judge_model`, send llama.cpp-style top-level `json_schema`, omit `reasoning`, and use a simplified occupied-position grid schema; OpenAI calls and optional local fallback use `ai_judge_model`. OpenAI Responses `text.format` was tested against llama.cpp and accepted but not enforced. Taste recompute and image-description learning remain OpenAI-backed.

Feedback learning works through `TasteService.recompute()`, not a per-vote weight nudge: user likes/dislikes accumulate as `TasteSampleState` rows with their clothing bucket; `recompute` calls `describe_reference_images` on wardrobe images, then `build_taste_profile` with observations + offer examples + the manual note, producing a new `TasteProfile` with per-item rubrics, prompts, and search drafts. The profile is marked dirty whenever feedback is recorded, as a signal that recompute is due.

### Vinted integration

`integrations/vinted/client.py` is a live cookie-authenticated HTTP adapter (not a fixture stub) with three concerns kept separate per `docs/scraping-strategy.md`: session validation (cached for 15 min via `SESSION_HEALTH_TTL`), search execution, and listing normalization. Parser behavior is locked by fixture-driven tests — when changing parsing, add or update a saved fixture in `backend/tests/` rather than relaxing assertions.

**Only the `de` region is supported and ever needs to be.** Do not add multi-region handling, region-conditional logic, or fixtures for other regions.

### Telegram integration

Outbound delivery is driven by `AlertDeliveryState` rows: candidates that score `decision == "alert"` get a delivery row queued, the worker picks them up, calls `TelegramClient`, and writes back `sent` / retry-pending (up to `DELIVERY_MAX_ATTEMPTS = 3` with backoff `DELIVERY_RETRY_DELAYS`) / `failed`. Inbound feedback comes through `POST /api/telegram/webhook`; `TelegramFormatter.parse_feedback_callback_data` decodes the `feedback:<delivery_id>:<verdict>` payload, and `AppState` resolves the delivery back to a candidate and runs the same `apply_feedback` path as the web UI. `TELEGRAM_WEBHOOK_SECRET`, if set, is enforced on inbound requests.

### Configuration & storage layout

`vsniper.core.config.Settings` (pydantic-settings) loads from `.env` and resolves relative paths against the repo root (discovered by walking up to `docker-compose.yml`). All filesystem state lives under `storage/`:

- `storage/sqlite/vsniper.db` — app DB (WAL mode, 5s busy timeout, configured in `core/database.py`)
- `storage/uploads/` — preference reference images
- `storage/cache/` — cached listing assets

Schema migrations are managed by Alembic (`backend/alembic.ini`, `backend/alembic/`). `init_database()` runs `alembic upgrade head` on startup, so the DB is brought to the current revision automatically. To add a migration after changing a SQLAlchemy model: `uv run --project backend --extra dev alembic revision --autogenerate -m "your message"`, review the generated file under `backend/alembic/versions/`, then commit.

### Web app

React 18 + Vite + TypeScript + React Router + TanStack Query. One page per top-level concept (`DashboardPage`, `SearchesPage`, `MyTastePage`, `CandidatesPage`, `CostsPage`, `SettingsPage`), all sharing `web/src/lib/api.ts` as the single fetch wrapper. All backend calls hit `/api/*` and rely on the Vite proxy (or the deployed reverse proxy) — do not hardcode the backend origin.

- **MyTastePage** — wardrobe samples grouped by clothing item, upload/edit controls for the clothing bucket, offer like/dislike history, free-text taste note, recompute button, item-specific profile tabs, and generated search drafts with save-to-searches action.
- **CostsPage** — AI spend broken down by stage (judge / learning) and time window (24 h / 7 d / 30 d / all time), backed by `CandidateService.get_ai_cost_stats()`.
