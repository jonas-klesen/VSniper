Blog post [here](https://jonas-klesen.de/blog/vsniper/).

# VSniper

A Vinted clothes-discovery app that learns your taste from wardrobe photos and feedback, scores new listings with vision models, and sends strong matches to Telegram.

## What it does

- runs saved Vinted searches using an authenticated session
- learns item-specific taste profiles from wardrobe photos, notes, and offer feedback
- scores candidate listings with explainable 1–100 judgments
- groups taste into six clothing buckets: shoes, trousers, warm/mid/cold upper-body pieces, and headwear
- sends high-scoring matches to Telegram and records like/dislike feedback
- tracks AI usage and estimated costs

Each upload, search, candidate, and feedback sample belongs to a clothing bucket. Recomputing your taste creates a separate prompt, rubric, and suggested Vinted search for each bucket while still carrying broader style preferences across categories.

## Quick start

Copy the example environment file and add the required credentials:

```bash
cp .env.example .env
```

### Full stack

```bash
docker compose up --build
```

This starts the API, worker, and web app with persistent state under `storage/`. Open `http://127.0.0.1:5173` for the Vite app or `http://127.0.0.1` for the local gateway.

The development stack is unauthenticated and bound to localhost. Do not expose it directly to a network.

### Backend

```bash
uv sync --project backend --extra dev
uv run --project backend uvicorn vsniper.api.main:app --reload --app-dir backend/src
```

Run the worker in a second terminal:

```bash
uv run --project backend python -m vsniper.worker.scheduler
```

### Web

```bash
cd web
npm install
npm run dev
```

## Local judge model

By default, the worker judges listings with a local vision model. It calls an
OpenAI-compatible server at `LOCAL_VLM_BASE_URL`. The code default is
`http://127.0.0.1:8080/v1`. `.env.example` uses
`http://host.docker.internal:8080/v1`, so the Docker containers can reach a
server on the host. VSniper sends `POST /v1/responses` with a JSON schema, so
use a recent llama.cpp build.

The local judge uses **LiquidAI LFM3-VL-2B Q4_0**, with its Q8_0 vision
projector. Use llama.cpp v0.8.0 or newer:

```bash
llama-server -hf LiquidAI/LFM3-VL-2B-GGUF:Q4_0 --alias lfm25-vl-clothing \
  --host 0.0.0.0 --port 8080 --ctx-size 4096 --parallel 1 \
  --image-min-tokens 64 --image-max-tokens 512 --reasoning off
```

- `-hf` downloads the model and its vision projector on the first start.
- `--host 0.0.0.0` lets the Docker containers connect. Do not expose port 8080
  to a public network.
- Check the server with `curl http://127.0.0.1:8080/health`.
- `--reasoning off` is required for direct structured judgments; default thinking
  can exhaust the output budget before producing JSON.
- `lfm25-vl-clothing` is a retained compatibility alias, not the model version.
  Both local model defaults use it. These
  variables only seed a new database. After that, change models on the
  **AI Models** page.

To skip the local model, set `AI_JUDGE_PROVIDER=openai`. The judge then uses
`AI_JUDGE_MODEL` with `AI_API_KEY`.

## Typical workflow

1. Add wardrobe photos and assign each one to a clothing bucket.
2. Write an optional taste note and recompute the taste profile.
3. Review the generated search drafts and save the ones you want to run.
4. Vote on candidates to improve the next recompute.
5. Send high-scoring matches to Telegram.

## Configuration

Use [`.env.example`](.env.example) as the configuration reference. The main credentials are:

- `VINTED_COOKIE` for live Vinted searches
- `VINTED_BROWSER_PROXY_URL` for proxy-backed Add-by-URL imports; use a sticky
  residential endpoint so browser verification survives between requests
- `AI_API_KEY` for AI-backed taste learning and supported judging modes
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for alerts and feedback
- `TELEGRAM_WEBHOOK_URL` and optional `TELEGRAM_WEBHOOK_SECRET` for Telegram callbacks

SQLite data, uploads, and cached listing assets are stored under `storage/`.

## Deployment

Production uses `docker-compose.coolify.yml`. In Coolify:

1. Create a Docker Compose resource using `docker-compose.coolify.yml`.
2. Add the variables from `.env.example` and set `BASIC_AUTH_USERS`. Generate a value with `python3 scripts/generate_auth.py`.
3. Assign your domain to the `web` service on port `80`.
4. Set `TELEGRAM_WEBHOOK_URL` to `https://<your-domain>/api/telegram/webhook` and register the webhook.

Coolify handles the public proxy and TLS. The web container serves the built app through nginx, proxies `/api` to the backend, and requires basic authentication for the dashboard and API.
Before deploying, configure a sticky residential proxy for the persistent
Vinted browser:

```env
VINTED_BROWSER_PROXY_URL=http://username:password@proxy-host:port
```

If Vinted requests browser verification during an Add-by-URL import, use the
action shown in the modal to open `/vinted-browser/`, solve it in the persistent
browser, and retry the import.

### Updating production

A push alone does not redeploy. Trigger the deployment in Coolify after you push.
Operator-specific access details live in an untracked `DEPLOY.local.md`.

## Development checks

```bash
uv run --project backend --extra dev pytest backend/tests
uv run --project backend --extra dev ruff check backend/src backend/tests
uv run --project backend --extra dev mypy backend/src
cd web && npm run build
```
