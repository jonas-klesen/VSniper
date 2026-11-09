# Architecture overview

## Clean-room boundary

`fbm-sniper-community/` remains a reference-only artifact. The new application runtime is built from scratch around Python services and a React web app.

## Runtime services

### API (`backend/`)

A FastAPI service exposing:

- search management
- filters and preference profile management
- transparency endpoints for prompts, extracted features, and score traces
- candidate review endpoints
- stats and health endpoints
- Telegram webhook endpoints

### Worker (`backend/`)

A separate process responsible for:

- running recurring Vinted scans
- deduplicating candidates
- extracting structured clothing features
- computing deterministic scores with explanation traces
- queueing Telegram alerts
- refreshing learning snapshots when feedback arrives

### Web (`web/`)

A React dashboard for:

- configuring searches and filters
- uploading wardrobe/reference images with a clothing item bucket
- viewing prompt templates, model outputs, and score breakdowns
- reviewing candidates and voting like/dislike
- testing Telegram configuration and monitoring worker health

## Clothing item buckets

Taste is modeled separately for seven clothing item buckets:

- `schuhe` / Schuhe: shoes and sneakers.
- `hosen` / Hosen: trousers, jeans, cargos, shorts, and other legwear.
- `obenrum_warm` / Obenrum Warm: warm-weather upper-body pieces such as T-shirts and short-sleeve shirts.
- `obenrum_mittel` / Obenrum Mittel: mid-layer upper-body pieces such as longsleeves and light pullovers.
- `obenrum_kalt` / Obenrum Kalt: cold-weather knitwear such as heavy pullovers and cardigans.
- `jacken` / Jacken: jackets and coats, especially warm winter jackets.
- `kopf` / Kopf: headwear, especially funny or weird baseball caps.

Every wardrobe sample, generated search draft, saved search, candidate, reference observation, and feedback example carries one of these buckets.

## Preference model design

The system keeps two explicit layers:

1. **Filters** — hard constraints such as size, brand, category, price, and region.
2. **Preferences** — soft taste signals extracted from images, notes, and feedback.

`TasteProfile` is the global recompute artifact. It contains one `ClothingItemTasteProfile` per bucket, each with its own prompt, scoring rubric, transparency labels, and exactly one editable Vinted search draft. The top-level profile remains a global aesthetic summary.

Cross-item leakage is intentional: each item profile is built primarily from evidence in its own bucket, then receives `cross_item_influence` from the rest of the wardrobe and feedback history. That lets, for example, playful color, vintage outdoor cues, or weird humor learned from hats influence trouser or shoe judging without forcing exact garment matches across buckets.

At scan time a saved search uses its `clothing_item` to select the matching item taste profile. VLM judging and Telegram feedback both operate against that item-specific prompt and rubric. Feedback examples inherit the clothing bucket from the source search, so later recomputes can separate target-item evidence from cross-item influence.

Saved searches are Vinted-category-scoped from the same `clothing_item`. The backend injects a resolvable category filter when one is missing, validates manually supplied category aliases against the selected bucket, and applies the same defaults to legacy searches at run time. Taste recompute prompts include the allowed Vinted category aliases/IDs so generated drafts choose from known values, but backend post-processing remains the source of truth.

## Local judge model

Scan-time judging can run through either OpenAI or a local OpenAI-compatible VLM. Every listing is judged in its own request: one square image (the main photo, or up to four photos packed together) plus the item-specific prompt. Local support is deliberately narrow: it is for judging only.

When the judge provider is `local`, `OpenAITasteClient.judge_candidate()` calls llama.cpp's `POST {base_url}/chat/completions` (its `/responses` endpoint returns no logprobs) with:

- the search-constant prompt context as a leading user message, so llama.cpp can reuse it as a cached prefix
- `response_format` JSON schema output and no `reasoning` field
- `logprobs` with the top 20 alternatives

The local score is the probability-weighted value of the score token rather than the sampled number. A small judge spreads its belief over several rubric band edges, and near-greedy sampling otherwise collapses most listings onto the single most likely edge. When the logprobs do not yield exactly one score, the sampled score is used. If a fallback model is configured, a failed primary call retries once through it; a listing that still fails gets a failed `ScoreTrace` in `SearchService._judge_listing()` without affecting other listings.

Cerebras fallback is judge-only. It uses `POST {CEREBRAS_API_BASE_URL}/chat/completions` with a `gemma-4-31b` default, base64 image data URIs, and strict `response_format` JSON schema output.

Taste recompute remains OpenAI-backed. `describe_reference_images()`, `describe_candidate_image()`, and `build_taste_profile()` continue to use the OpenAI Responses API and the learning model settings.

## Persistence roadmap

The application now persists to a local SQLite database file in mounted storage so it runs the same way in Docker Compose for local development and deployment. The persistence architecture is:

- SQLite for settings, searches, candidates, feedback, per-item taste samples, and learning snapshots
- normal mounted storage for uploaded images, cached assets, and the database file itself
- no required external database or cache services for the current stack
