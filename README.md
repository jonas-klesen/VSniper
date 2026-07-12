# vsniper

A clean-room Vinted sniper rewrite focused on clothes discovery, multimodal taste learning, and shared backend infrastructure for both a web app and a Telegram bot.

## Project status

This repository contains the active clean-room rewrite. The cloned `fbm-sniper-community/` directory is kept only as a reference source for concepts and patterns. It is **not** part of the runtime architecture.

The core pipeline is fully wired end to end:

- ✅ SQLite-backed searches, candidates, and feedback. Alembic manages schema migrations automatically on startup.
- ✅ Vinted search uses a live cookie-backed HTTP adapter with upstream session validation, listing normalization, and parser fixtures covered by backend tests.
- ✅ VLM judging sends all fetched candidates with usable images as 1-, 4-, or 9-image contact-sheet batches to either OpenAI or a local OpenAI-compatible `/v1/responses` VLM and returns a structured 1–10 score, explanation, labels, and concerns per candidate.
- ✅ TasteService orchestrates full taste profile recomputes: wardrobe images → `ReferenceObservation[]` via vision, combined with liked/disliked offer examples and a free-text note → `TasteProfile` with one item-specific prompt/rubric profile and one search draft per clothing bucket.
- ✅ Telegram outbound delivery processes queued `AlertDeliveryState` records, sends via the Bot API, and marks deliveries as sent / retry-pending / failed (up to 3 attempts with backoff).
- ✅ Telegram inline feedback callbacks flow through the webhook route, resolve to alert deliveries, and feed the same feedback path as the web UI, marking the taste profile dirty for next recompute.
- ✅ AI cost tracking logs every AI call (operation, model, tokens, estimated cost) to `AiUsageEvent`; the CostsPage surfaces spend by stage and time window.

## Planned services

- `backend/` — FastAPI API, worker entrypoints, domain logic, integrations
- `web/` — React + TypeScript admin UI
- `docker-compose.yml` — local development stack (Traefik + live-reload API + Vite dev server), bound to localhost
- `docker-compose.coolify.yml` — production stack for Coolify (nginx-served build, basic-auth, no host ports)
- `docs/` — architecture, scraping strategy, clean-room notes
- `storage/` — local upload/cache/SQLite volumes for development and deployment

## First-run goals

- configure Vinted search filters
- upload wardrobe reference images, assign each image to a clothing item bucket, and write a taste note
- trigger a taste profile recompute to generate item-specific prompts, rubrics, and one search draft per clothing style
- review candidate listings and vote like/dislike to keep the taste profile current
- route high-scoring matches to Telegram

## Clothing item buckets

The taste system uses six fixed clothing buckets: Schuhe (`schuhe`), Hosen (`hosen`), Obenrum Warm (`obenrum_warm`, T-shirts and short-sleeve shirts), Obenrum Mittel (`obenrum_mittel`, longsleeves and light pullovers), Obenrum Kalt (`obenrum_kalt`, heavier pullovers and jackets), and Kopf (`kopf`, funny or weird baseball caps).

Each wardrobe upload, search, candidate, feedback sample, and generated draft is tied to one bucket. Recompute produces a separate taste prompt per bucket, while cross-item influence lets broader wardrobe taste leak across categories.

Saved searches are also automatically scoped to the matching Vinted category aliases for their bucket. For example, `obenrum_warm` searches include the Vinted `tops` / `t-shirts` category aliases, while `hosen` searches include trousers, jeans, and shorts. Generated drafts receive these filters during recompute, and manually saved searches get the same backend validation before scans run.

## Clean-room rule

Use the contents of `fbm-sniper-community/` only as human-readable reference material. Do not import code from it into the new runtime.

## Quick start

The backend persists app state into a SQLite file under `storage/sqlite/` and keeps uploads/cached assets in the normal `storage/` tree.

### Backend

- copy `.env.example` to `.env` and fill in any secrets you already have
- create a virtual environment
- install `backend/` in editable mode
- run `uvicorn vsniper.api.main:app --reload --app-dir backend/src`

### Web

- install dependencies inside `web/`
- run `npm run dev`

### Full stack (local)

- copy `.env.example` to `.env`
- `docker compose up --build`

This brings up Traefik, the live-reload API, the worker, and the Vite dev server. The whole stack is **unauthenticated and bound to `127.0.0.1`** — it is for local dev only and must never be exposed to a network. Reach it at:

- `http://127.0.0.1:5173` — Vite dev server (hot reload), or
- `http://127.0.0.1:80` — the Traefik gateway (same routing as prod, minus auth)

## Deployment (Coolify)

Production uses `docker-compose.coolify.yml`, which differs from the dev stack in three ways: Coolify's own proxy handles the domain and TLS (no bundled Traefik), no host ports are published, and `web` serves a **built bundle via nginx** (`web/Dockerfile.prod`) instead of the dev server. nginx reverse-proxies `/api` to the backend and enforces HTTP basic-auth on the dashboard and `/api`, while leaving `/api/telegram/webhook` and `/healthz` public. The web container **refuses to start** if `BASIC_AUTH_USERS` is unset, so the API and its secrets can never be served unauthenticated.

Steps:

1. In Coolify: **New Resource → Docker Compose**, pointing at `docker-compose.coolify.yml`.
2. Set environment variables in the Coolify UI (Coolify injects them into every service): everything from `.env.example` plus `BASIC_AUTH_USERS`. Generate credentials with `python3 scripts/generate_auth.py` (format: `user:{SHA}base64(sha1(password))`, comma-separated for multiple users).
3. Assign your domain to the `web` service (container port `80`).
4. Set `TELEGRAM_WEBHOOK_URL` to `https://<your-domain>/api/telegram/webhook` and register it.

App state persists in the named `storage` volume (SQLite DB, uploads, cache).

## Environment

Use the checked-in `.env.example` as the source of truth for local configuration.

- `VINTED_COOKIE` is required for live search execution and is validated against the upstream Vinted session endpoint.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are required for outbound alert delivery and inbound callback acknowledgements.
- `TELEGRAM_WEBHOOK_URL` is the public HTTPS callback URL used by the webhook registration tooling.
- `TELEGRAM_WEBHOOK_SECRET` is optional but recommended; when set, inbound webhook requests must include Telegram's secret header.
- `AI_API_KEY` is required for OpenAI-backed judging, OpenAI fallback, and taste profile recomputes. Recompute/learning still uses OpenAI.
- `CEREBRAS_API_KEY` is required only when `AI_JUDGE_FALLBACK_PROVIDER=cerebras`.
- `AI_JUDGE_PROVIDER` selects the scan-time judge backend. It defaults to `local`; use `openai` to judge directly through OpenAI. Runtime overrides are stored in SQLite and editable via SettingsPage.
- `LOCAL_JUDGE_MODEL` is the local model id used when `AI_JUDGE_PROVIDER=local`, for example `gemma4-12b-quality`.
- `AI_JUDGE_MODEL` is the OpenAI judge model used when `AI_JUDGE_PROVIDER=openai`, and also the fallback model when local fallback is enabled.
- `AI_JUDGE_FALLBACK_PROVIDER` selects what failed local judge calls retry through: `none`, `openai`, or `cerebras`. `AI_JUDGE_ALLOW_OPENAI_FALLBACK` is kept only as a legacy compatibility alias for `openai`.
- `CEREBRAS_JUDGE_MODEL` defaults to `gemma-4-31b` and is used only for Cerebras fallback.
- `LOCAL_VLM_BASE_URL` is used only when local judging is active and defaults to `http://127.0.0.1:8080/v1`. The server must support `POST /v1/responses` with multimodal `input_image` parts and llama.cpp-style top-level `json_schema`.
- `AI_LEARN_MODEL` configures OpenAI taste learning/recompute. Local VLM support is judging-only.
- `UPLOAD_DIR` and `CACHE_DIR` back local storage for uploaded reference images and cached candidate images.

### Local judge VLM

Local judging is intended for llama.cpp-compatible servers that expose an OpenAI-compatible base URL. The app only calls `POST {LOCAL_VLM_BASE_URL}/responses`; it never uses `/chat/completions`.

Minimal `.env` example:

```dotenv
AI_JUDGE_PROVIDER=local
LOCAL_JUDGE_MODEL=gemma4-12b-quality
AI_JUDGE_MODEL=gpt-5.4-mini
AI_JUDGE_ALLOW_OPENAI_FALLBACK=false
AI_JUDGE_FALLBACK_PROVIDER=none
CEREBRAS_JUDGE_MODEL=gemma-4-31b
LOCAL_VLM_BASE_URL=http://127.0.0.1:8080/v1
```

The local judging request sends:

- a single contact-sheet image containing one batch of fetched candidates
- the item-specific taste prompt, rubric, metadata, and calibration anchors
- top-level `json_schema` to constrain the response to score/explanation/labels/concerns

Caveats:

- The local model is only used for scan-time candidate judging. Wardrobe image description, feedback evidence capture, and taste profile recompute still require OpenAI settings.
- Reasoning controls are deliberately omitted for local calls; the local server accepted reasoning fields in probes, but did not expose evidence that they changed behavior.
- OpenAI Responses `text.format.json_schema` is not used for local calls. Probes showed llama.cpp accepts that field but does not enforce it; top-level `json_schema` does enforce output shape.
- The local schema is intentionally simpler than the OpenAI schema and only requires occupied grid positions, avoiding `anyOf` because llama.cpp documents limitations there.
- Unknown local model names have no pricing table entry, so cost tracking records token usage but reports `$0` estimated cost unless pricing is added.

## Next implementation steps

1. deepen live Vinted parser coverage with more saved fixtures (de region only)
2. optionally persist Telegram message metadata for post-click message edits and richer delivery analytics
