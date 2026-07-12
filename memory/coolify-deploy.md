---
name: coolify-deploy
description: How vsniper is deployed (Coolify production vs local dev) and the open follow-up
metadata:
  type: project
---

Production target is **Coolify** using `docker-compose.coolify.yml`: no bundled Traefik (Coolify's own proxy terminates TLS + routes the domain assigned to the `web` service on port 80), no host port mappings, and `web` is built via `web/Dockerfile.prod` (nginx serving the built SPA + reverse-proxying `/api` to `api:8000`). nginx enforces basic-auth from `$BASIC_AUTH_USERS` (`web/docker-entrypoint.d/40-htpasswd.sh`, fail-closed if unset) while leaving `/api/telegram/webhook` and `/healthz` public.

`docker-compose.yml` remains the **local dev** stack. As of 2026-06-09 it is fully unauthenticated AND bound to `127.0.0.1` only (Traefik gateway + Vite both loopback) — basic-auth was deliberately stripped from dev since it's localhost-only.

The two compose files duplicate the api/worker `build`+healthcheck blocks. Compose `include` was tried for dedup but FAILS: it can't override imported services ("conflicts with imported resource"), and the multi-`-f` merge that can override fights Coolify's single-file model. Kept self-contained with "KEEP IN SYNC" comments instead — don't retry `include`.

**Why:** user wants to deploy on Coolify but still keep developing/running locally.
**How to apply:** Done — auth stripped in dev (#1), README has Local vs Coolify sections (#3), prod web image build+routing+auth+fail-closed verified locally (#5). Still PENDING: a one-command bootstrap (`make dev`/`make deploy` or script that seeds `.env` + generates BASIC_AUTH_USERS) (#4).
