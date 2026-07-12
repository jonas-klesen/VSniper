# AI Models Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace scattered free-text model inputs with a centralized AI model registry — a new DB table, CRUD API, dedicated frontend page, and reusable model selector component.

**Architecture:** A new `ai_models` table stores model entries (provider + name + effort). `AppSettingsState` gains `judge_model_id`, `learn_model_id`, `observation_model_id`, `fallback_model_id` columns that reference `ai_models.id`. The frontend gets a new `/ai-models` page for managing models and a `ModelSelect` dropdown component used everywhere models are selected.

**Tech Stack:** Python 3.12, SQLAlchemy, Alembic, FastAPI, Pydantic, React 18, TypeScript, Vite, TanStack Query

## Global Constraints

- Only `de` Vinted region — no multi-region logic
- SQLite with WAL mode, 5s busy timeout
- Alembic migrations via `uv run --project backend --extra dev alembic revision --autogenerate`
- All model references use ULID IDs, not names
- `display_name` auto-generated as `"{model_name} ({Provider})"`
- API keys are masked in GET responses
- Delete a model fails if it's referenced by any setting

---

### Task 1: Backend — DB model, migration, contracts

**Covers:** S3

**Files:**
- Modify: `backend/src/vsniper/db/models.py`
- Create: `backend/alembic/versions/20260630a005_add_ai_models_registry.py`
- Modify: `backend/src/vsniper/domain/contracts.py`

**Interfaces:**
- Produces: `AiModelConfig` DB model, `AiModelConfig`/`AiModelCreate`/`AiModelUpdate` contracts, Alembic migration

- [ ] **Step 1: Add `AiModelConfig` DB model**

Add to `backend/src/vsniper/db/models.py` after `AppSettingsState`:

```python
class AiModelConfig(Base):
    __tablename__ = "ai_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
```

- [ ] **Step 2: Add model ID reference columns to `AppSettingsState`**

Add these columns to `AppSettingsState` in `models.py`:

```python
judge_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
learn_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
observation_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
fallback_model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

- [ ] **Step 3: Generate Alembic migration**

Run: `uv run --project backend --extra dev alembic revision --autogenerate -m "add ai_models registry and model_id references"`

Edit the generated file to:
1. Create `ai_models` table
2. Add the four `*_model_id` columns to `app_settings`
3. Add a data migration that seeds default models from current settings values:
   - `gpt-5.4-mini (OpenAI) · low` — judge
   - `gemma-4-31b (Cerebras) · low` — fallback
   - `gpt-5.5 (OpenAI) · medium` — learn
   - `gemma4-12b-quality (Local) · low` — local judge/observation
4. Set `judge_model_id`, `learn_model_id`, `observation_model_id`, `fallback_model_id` to reference the seeded models

Use `op.bulk_insert` for seeding. Generate ULIDs inline (e.g. `import ulid` or use a simple timestamp-based ID).

- [ ] **Step 4: Add contract types**

Add to `backend/src/vsniper/domain/contracts.py`:

```python
class AiModelConfig(BaseModel):
    id: str
    provider: Literal["openai", "cerebras", "local"]
    model_name: str
    reasoning_effort: Literal["low", "medium", "high"]
    display_name: str
    is_default: bool
    created_at: int
    updated_at: int

class AiModelCreate(BaseModel):
    provider: Literal["openai", "cerebras", "local"]
    model_name: str = Field(min_length=1, max_length=128)
    reasoning_effort: Literal["low", "medium", "high"]
    is_default: bool = False

class AiModelUpdate(BaseModel):
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    is_default: bool | None = None
```

- [ ] **Step 5: Update `SettingsSnapshot` contract**

Replace in `SettingsSnapshot`:
- Remove: `ai_judge_provider`, `ai_judge_model`, `local_judge_model`, `ai_judge_allow_openai_fallback`, `ai_judge_fallback_provider`, `cerebras_judge_model`, `ai_judge_reasoning_effort`, `ai_learn_model`, `ai_learn_reasoning_effort`, `ai_observation_provider`
- Add: `judge_model_id: str | None = None`, `learn_model_id: str | None = None`, `observation_model_id: str | None = None`, `fallback_model_id: str | None = None`, `models: list[AiModelConfig] = Field(default_factory=list)`

- [ ] **Step 6: Update `SettingsUpdate` contract**

Replace in `SettingsUpdate`:
- Remove: `ai_judge_provider`, `ai_judge_model`, `local_judge_model`, `ai_judge_allow_openai_fallback`, `ai_judge_fallback_provider`, `cerebras_judge_model`, `ai_judge_reasoning_effort`, `ai_learn_model`, `ai_learn_reasoning_effort`, `ai_observation_provider`
- Add: `judge_model_id: str | None = None`, `learn_model_id: str | None = None`, `observation_model_id: str | None = None`, `fallback_model_id: str | None = None`

- [ ] **Step 7: Run migration and verify**

Run: `uv run --project backend --extra dev alembic upgrade head`
Expected: migration applies cleanly, `ai_models` table has 4 seeded rows, `app_settings` has the new columns populated.

- [ ] **Step 8: Commit**

```bash
git add backend/src/vsniper/db/models.py backend/src/vsniper/domain/contracts.py backend/alembic/versions/
git commit -m "feat: add ai_models registry table and model ID references"
```

---

### Task 2: Backend — CRUD API + settings service updates

**Covers:** S2, S4

**Files:**
- Create: `backend/src/vsniper/api/routes/ai_models.py`
- Modify: `backend/src/vsniper/api/main.py`
- Modify: `backend/src/vsniper/services/_mapping.py`
- Modify: `backend/src/vsniper/services/search_service.py`
- Modify: `backend/src/vsniper/core/sqlite_state.py`

**Interfaces:**
- Consumes: `AiModelConfig` DB model, `AiModelCreate`/`AiModelUpdate` contracts from Task 1
- Produces: CRUD endpoints at `/api/ai-models`, updated `settings_to_contract()`, updated `update_app_settings()`

- [ ] **Step 1: Create AI models route**

Create `backend/src/vsniper/api/routes/ai_models.py`:

```python
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from vsniper.core.database import session_scope
from vsniper.db.models import AiModelConfig as AiModelRow, AppSettingsState
from vsniper.domain.contracts import AiModelConfig, AiModelCreate, AiModelUpdate

router = APIRouter(prefix="/api/ai-models", tags=["ai-models"])

_PROVIDER_DISPLAY: dict[str, str] = {
    "openai": "OpenAI",
    "cerebras": "Cerebras",
    "local": "Local",
}


def _row_to_contract(row: AiModelRow) -> AiModelConfig:
    return AiModelConfig(
        id=row.id,
        provider=row.provider,  # type: ignore[arg-type]
        model_name=row.model_name,
        reasoning_effort=row.reasoning_effort,  # type: ignore[arg-type]
        display_name=row.display_name,
        is_default=bool(row.is_default),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _display_name(model_name: str, provider: str) -> str:
    label = _PROVIDER_DISPLAY.get(provider, provider.title())
    return f"{model_name} ({label})"


@router.get("", response_model=list[AiModelConfig])
def list_models() -> list[AiModelConfig]:
    with session_scope() as session:
        rows = session.execute(select(AiModelRow).order_by(AiModelRow.is_default.desc(), AiModelRow.created_at)).scalars().all()
        return [_row_to_contract(r) for r in rows]


@router.post("", response_model=AiModelConfig, status_code=201)
def create_model(payload: AiModelCreate) -> AiModelConfig:
    now = int(time.time() * 1000)
    model_id = f"aim_{int(time.time() * 1000000)}"
    with session_scope() as session:
        if payload.is_default:
            session.execute(
                AiModelRow.__table__.update().where(AiModelRow.is_default == True).values(is_default=False)  # noqa: E712
            )
        row = AiModelRow(
            id=model_id,
            provider=payload.provider,
            model_name=payload.model_name,
            reasoning_effort=payload.reasoning_effort,
            display_name=_display_name(payload.model_name, payload.provider),
            is_default=payload.is_default,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return _row_to_contract(row)


@router.put("/{model_id}", response_model=AiModelConfig)
def update_model(model_id: str, payload: AiModelUpdate) -> AiModelConfig:
    now = int(time.time() * 1000)
    with session_scope() as session:
        row = session.get(AiModelRow, model_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Model not found")
        if payload.model_name is not None:
            row.model_name = payload.model_name
        if payload.reasoning_effort is not None:
            row.reasoning_effort = payload.reasoning_effort
        if payload.is_default is not None:
            if payload.is_default:
                session.execute(
                    AiModelRow.__table__.update().where(AiModelRow.is_default == True).values(is_default=False)  # noqa: E712
                )
            row.is_default = payload.is_default
        row.display_name = _display_name(row.model_name, row.provider)
        row.updated_at = now
        session.flush()
        return _row_to_contract(row)


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: str) -> None:
    with session_scope() as session:
        row = session.get(AiModelRow, model_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Model not found")
        # Check if referenced by any setting
        settings = session.execute(select(AppSettingsState)).scalars().first()
        if settings is not None:
            referenced = {
                settings.judge_model_id,
                settings.learn_model_id,
                settings.observation_model_id,
                settings.fallback_model_id,
            }
            if model_id in referenced:
                raise HTTPException(status_code=409, detail="Model is referenced by application settings and cannot be deleted")
        session.delete(row)
```

- [ ] **Step 2: Register route in main.py**

Add to `backend/src/vsniper/api/main.py`:

```python
from vsniper.api.routes.ai_models import router as ai_models_router
# ...
app.include_router(ai_models_router)
```

- [ ] **Step 3: Update `settings_to_contract()` in `_mapping.py`**

Replace the body of `settings_to_contract()` to:
1. Query all `AiModelConfig` rows
2. Build the `models` list
3. Map model ID fields instead of the old string fields
4. Compute `ai_configured`, `judge_configured`, `learning_configured` from the referenced model entries

```python
def settings_to_contract(model: AppSettingsState) -> SettingsSnapshot:
    runtime = get_settings()
    telegram_bot_token = model.telegram_bot_token or runtime.telegram_bot_token
    telegram_chat_id = model.telegram_chat_id or runtime.telegram_chat_id
    telegram_webhook_url = model.telegram_webhook_url or runtime.telegram_webhook_url
    telegram_webhook_secret = model.telegram_webhook_secret or runtime.telegram_webhook_secret
    vinted_configured, telegram_configured, _ = integration_configuration(
        db_cookie=model.vinted_cookie,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
    )
    # Load AI models for the snapshot
    from vsniper.db.models import AiModelConfig as AiModelRow
    from vsniper.core.database import session_scope
    ai_models = []
    # We're already in a session context from the caller, so use the session directly
    # The caller passes the model, we need the session to query models
    # Actually, we need to refactor to accept session or query here
    # For now, open a nested read
    with session_scope() as s:
        rows = s.execute(select(AiModelRow).order_by(AiModelRow.is_default.desc(), AiModelRow.created_at)).scalars().all()
        ai_models = [
            AiModelConfig(
                id=r.id, provider=r.provider, model_name=r.model_name,
                reasoning_effort=r.reasoning_effort, display_name=r.display_name,
                is_default=bool(r.is_default), created_at=r.created_at, updated_at=r.updated_at,
            )
            for r in rows
        ]
    
    model_by_id = {m.id: m for m in ai_models}
    judge_model = model_by_id.get(model.judge_model_id or "")
    learn_model = model_by_id.get(model.learn_model_id or "")
    
    ai_configured = len(ai_models) > 0
    judge_configured = judge_model is not None
    learning_configured = learn_model is not None and learn_model.provider == "openai"
    
    return SettingsSnapshot.model_validate({
        "vinted_region": model.vinted_region,
        "vinted_cookie": model.vinted_cookie or "",
        "vinted_refresh_token": model.vinted_refresh_token or "",
        "telegram_bot_token": telegram_bot_token,
        "telegram_chat_id": telegram_chat_id,
        "telegram_webhook_url": telegram_webhook_url,
        "telegram_webhook_secret": telegram_webhook_secret,
        "vinted_configured": vinted_configured,
        "telegram_configured": telegram_configured,
        "ai_configured": ai_configured,
        "judge_configured": judge_configured,
        "learning_configured": learning_configured,
        "judge_model_id": model.judge_model_id,
        "learn_model_id": model.learn_model_id,
        "observation_model_id": model.observation_model_id,
        "fallback_model_id": model.fallback_model_id,
        "models": [m.model_dump() for m in ai_models],
        "local_vlm_base_url": model.local_vlm_base_url,
        "vlm_grid_size": model.vlm_grid_size,
        "vlm_pack_multiple_listing_images": True if model.vlm_pack_multiple_listing_images is None else model.vlm_pack_multiple_listing_images,
        "vlm_judge_parallel_requests": model.vlm_judge_parallel_requests,
        "ai_judge_image_max_px": model.ai_judge_image_max_px,
        "alert_threshold": _coerce_alert_threshold(getattr(model, "alert_threshold", 9)),
        "session_health": model.session_health or build_session_health(region=model.vinted_region).model_dump(mode="json"),
    })
```

- [ ] **Step 4: Update `update_app_settings()` in `search_service.py`**

Replace the model-field update block (lines ~1190-1206) with:

```python
if payload.judge_model_id is not None:
    model.judge_model_id = payload.judge_model_id or None
if payload.learn_model_id is not None:
    model.learn_model_id = payload.learn_model_id or None
if payload.observation_model_id is not None:
    model.observation_model_id = payload.observation_model_id or None
if payload.fallback_model_id is not None:
    model.fallback_model_id = payload.fallback_model_id or None
```

Remove the old field assignments for `ai_judge_provider`, `ai_judge_model`, `local_judge_model`, `ai_judge_fallback_provider`, `ai_judge_allow_openai_fallback`, `cerebras_judge_model`, `ai_judge_reasoning_effort`, `ai_learn_model`, `ai_learn_reasoning_effort`, `ai_observation_provider`, `local_observation_model`.

- [ ] **Step 5: Update `_default_settings()` in `sqlite_state.py`**

Update `_default_settings()` to use model ID fields instead of the old string fields. Query seeded models to get their IDs for the defaults.

- [ ] **Step 6: Run lint and typecheck**

Run: `uv run --project backend --extra dev ruff check backend/src backend/tests && uv run --project backend --extra dev mypy backend/src`
Expected: PASS (or fix any issues)

- [ ] **Step 7: Commit**

```bash
git add backend/src/vsniper/api/routes/ai_models.py backend/src/vsniper/api/main.py backend/src/vsniper/services/_mapping.py backend/src/vsniper/services/search_service.py backend/src/vsniper/core/sqlite_state.py
git commit -m "feat: add AI models CRUD API and update settings to use model IDs"
```

---

### Task 3: Backend — Update OpenAI client to resolve model IDs

**Covers:** S8

**Files:**
- Modify: `backend/src/vsniper/integrations/openai/client.py`

**Interfaces:**
- Consumes: `AiModelConfig` DB model, model ID fields on `AppSettingsState`
- Produces: `judge_candidate_grid()` resolves model IDs to provider/name/effort at call time

- [ ] **Step 1: Add model resolution helper**

Add a helper function at module level in `client.py`:

```python
def _resolve_model(model_id: str | None, settings) -> tuple[str, str, str, str]:
    """Resolve a model ID to (provider, model_name, reasoning_effort, base_url).
    Falls back to legacy settings fields if model_id is None."""
    if model_id:
        from vsniper.db.models import AiModelConfig as AiModelRow
        from vsniper.core.database import session_scope
        with session_scope() as session:
            row = session.get(AiModelRow, model_id)
            if row:
                base_url = (
                    _setting_str(settings, "local_vlm_base_url", "http://127.0.0.1:8080/v1")
                    if row.provider == "local"
                    else _setting_str(settings, "cerebras_api_base_url", "https://api.cerebras.ai/v1")
                    if row.provider == "cerebras"
                    else ""
                )
                return row.provider, row.model_name, row.reasoning_effort, base_url
    # Fallback to legacy fields
    provider = _setting_str(settings, "ai_judge_provider", "local")
    model_name = _setting_str(settings, "ai_judge_model", "gpt-5.4-mini")
    effort = _setting_str(settings, "ai_judge_reasoning_effort", "low")
    base_url = _setting_str(settings, "local_vlm_base_url", "http://127.0.0.1:8080/v1")
    return provider, model_name, effort, base_url
```

- [ ] **Step 2: Update `judge_candidate_grid()` to use model resolution**

In `judge_candidate_grid()`, add a `model_id` parameter and use `_resolve_model()`:

```python
def judge_candidate_grid(
    self,
    *,
    taste_profile: TasteProfile,
    candidates: list[CandidateImageInput],
    liked_anchors: list[LabeledExample] | None = None,
    disliked_anchors: list[LabeledExample] | None = None,
    manual_note: str | None = None,
    model_id: str | None = None,
    fallback_model_id: str | None = None,
    model: str | None = None,  # kept for backward compat
    reasoning_effort: str | None = None,
    image_detail: str | None = None,
    ai_judge_provider: str | None = None,
    local_vlm_base_url: str | None = None,
    image_max_px: int = 512,
    pack_multiple_listing_images: bool = False,
    on_usage: UsageCallback | None = None,
) -> CandidateGridResult:
    # ... existing contact sheet logic ...
    provider, model_name, effort, base_url = _resolve_model(model_id, self.settings)
    if model:  # explicit override
        model_name = model
    if reasoning_effort:
        effort = reasoning_effort
    # ... rest of existing logic using provider, model_name, effort, base_url ...
```

- [ ] **Step 3: Update callers of `judge_candidate_grid()`**

Find all callers in `search_service.py` (the `_judge_candidates` method) and pass `model_id` and `fallback_model_id` from settings instead of the old `ai_judge_provider` / `model` / `reasoning_effort` params.

- [ ] **Step 4: Run tests**

Run: `uv run --project backend --extra dev pytest backend/tests -v`
Expected: existing tests pass (update any that reference old fields)

- [ ] **Step 5: Commit**

```bash
git add backend/src/vsniper/integrations/openai/client.py backend/src/vsniper/services/search_service.py
git commit -m "feat: update OpenAI client to resolve model IDs from registry"
```

---

### Task 4: Frontend — Types, API client, ModelSelect component

**Covers:** S6

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/lib/queryKeys.ts`
- Create: `web/src/components/ModelSelect.tsx`

**Interfaces:**
- Consumes: `AiModelConfig`, `SettingsSnapshot` from backend
- Produces: `ModelSelect` component, updated API client functions

- [ ] **Step 1: Add types to `types.ts`**

Add at the top of `web/src/types.ts`:

```typescript
export type AiModelConfig = {
  id: string;
  provider: 'openai' | 'cerebras' | 'local';
  model_name: string;
  reasoning_effort: 'low' | 'medium' | 'high';
  display_name: string;
  is_default: boolean;
  created_at: number;
  updated_at: number;
};

export type AiModelCreate = {
  provider: 'openai' | 'cerebras' | 'local';
  model_name: string;
  reasoning_effort: 'low' | 'medium' | 'high';
  is_default?: boolean;
};

export type AiModelUpdate = {
  model_name?: string;
  reasoning_effort?: 'low' | 'medium' | 'high';
  is_default?: boolean;
};
```

Update `SettingsSnapshot`:
- Remove: `ai_judge_provider`, `ai_judge_model`, `local_judge_model`, `ai_judge_allow_openai_fallback`, `ai_judge_fallback_provider`, `cerebras_judge_model`, `ai_judge_reasoning_effort`, `ai_learn_model`, `ai_learn_reasoning_effort`, `ai_observation_provider`
- Add: `judge_model_id: string | null`, `learn_model_id: string | null`, `observation_model_id: string | null`, `fallback_model_id: string | null`, `models: AiModelConfig[]`

- [ ] **Step 2: Update API client in `api.ts`**

Add to the `api` object:

```typescript
getAiModels: () => request<AiModelConfig[]>('/api/ai-models'),
createAiModel: (payload: AiModelCreate) =>
  request<AiModelConfig>('/api/ai-models', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
updateAiModel: (id: string, payload: AiModelUpdate) =>
  request<AiModelConfig>(`/api/ai-models/${id}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
deleteAiModel: (id: string) =>
  request<void>(`/api/ai-models/${id}`, { method: 'DELETE' }),
```

Update `saveSettings` payload to match new fields (remove old, add model IDs).

- [ ] **Step 3: Add query key**

Add to `queryKeys.ts`:

```typescript
aiModels: ['ai-models'] as const,
```

- [ ] **Step 4: Create `ModelSelect` component**

Create `web/src/components/ModelSelect.tsx`:

```tsx
import type { AiModelConfig } from '../types';

type Props = {
  models: AiModelConfig[];
  value: string | null;
  onChange: (id: string | null) => void;
  allowNone?: boolean;
  label?: string;
  helpText?: string;
};

export function ModelSelect({ models, value, onChange, allowNone = false, label, helpText }: Props) {
  return (
    <label>
      {label}
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
      >
        {allowNone && <option value="">None</option>}
        {models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.display_name} · {m.reasoning_effort}
          </option>
        ))}
      </select>
      {helpText && <span className="field-help">{helpText}</span>}
    </label>
  );
}
```

- [ ] **Step 5: Commit**

```bash
git add web/src/types.ts web/src/lib/api.ts web/src/lib/queryKeys.ts web/src/components/ModelSelect.tsx
git commit -m "feat: add frontend types, API client, and ModelSelect component"
```

---

### Task 5: Frontend — AI Models page

**Covers:** S5

**Files:**
- Create: `web/src/pages/AiModelsPage.tsx`

**Interfaces:**
- Consumes: `AiModelConfig`, `AiModelCreate`, `AiModelUpdate` types, API client functions from Task 4
- Produces: Full AI Models page with provider credentials and model registry

- [ ] **Step 1: Create the AI Models page**

Create `web/src/pages/AiModelsPage.tsx` with two sections:

1. **Provider Credentials** — three cards for OpenAI, Cerebras, Local with API key/URL inputs
2. **Model Registry** — table of models with add/edit/delete

The page fetches models via `api.getAiModels()` and settings via `api.getSettings()`. Provider credentials are saved as part of settings (via `api.saveSettings()`). Model CRUD uses the dedicated endpoints.

Key features:
- "Add model" form with provider dropdown, model name input, effort dropdown
- Inline edit for existing models
- Delete with confirmation (handle 409 if referenced)
- Display name auto-generated on backend
- Provider credentials shown in collapsible sections

- [ ] **Step 2: Run dev server and verify**

Run: `cd web && npm run dev`
Navigate to `/ai-models`. Verify:
- Page loads without errors
- Can add a model
- Can edit a model
- Can delete a model
- Provider credentials section renders

- [ ] **Step 3: Commit**

```bash
git add web/src/pages/AiModelsPage.tsx
git commit -m "feat: add AI Models page with provider credentials and model registry"
```

---

### Task 6: Frontend — Update Settings page, sidebar, router

**Covers:** S7

**Files:**
- Modify: `web/src/app/router.tsx`
- Modify: `web/src/app/App.tsx`
- Modify: `web/src/pages/SettingsPage.tsx`

**Interfaces:**
- Consumes: `ModelSelect` from Task 4, `AiModelsPage` from Task 5
- Produces: Updated sidebar with AI Models link, simplified Settings page

- [ ] **Step 1: Add route for AI Models page**

In `web/src/app/router.tsx`, add:

```tsx
import { AiModelsPage } from '../pages/AiModelsPage';
// ...
{ path: 'ai-models', element: <AiModelsPage /> },
```

Place it between `costs` and `settings` routes.

- [ ] **Step 2: Add sidebar link**

In `web/src/app/App.tsx`, add to the `links` array:

```tsx
import { Cpu } from 'lucide-react';
// ...
{ to: '/ai-models', label: 'AI Models', icon: Cpu },
```

Place it between `AI Costs` and `Settings`.

- [ ] **Step 3: Simplify Settings page**

Rewrite `SettingsPage.tsx` to:
1. Remove `SettingsFormState` fields for old model config
2. Add `judge_model_id`, `learn_model_id`, `observation_model_id`, `fallback_model_id` to form state
3. Replace provider/model/effort dropdowns with `ModelSelect` components
4. Remove the "Local model" section (moved to AI Models page)
5. Keep: Vinted session, Judge (threshold, grid, parallel, tile size, pack images, model selectors), Learn (model selector), Telegram

The `ModelSelect` component needs the models list from `settingsQuery.data.models`.

- [ ] **Step 4: Run dev server and verify**

Run: `cd web && npm run dev`
Verify:
- Sidebar shows "AI Models" link between "AI Costs" and "Settings"
- Settings page shows `ModelSelect` dropdowns instead of free-text inputs
- Selecting a model updates the form state correctly
- Save works with the new payload shape

- [ ] **Step 5: Commit**

```bash
git add web/src/app/router.tsx web/src/app/App.tsx web/src/pages/SettingsPage.tsx
git commit -m "feat: add AI Models to sidebar, simplify Settings page with model selectors"
```

---

### Task 7: Integration testing and cleanup

**Covers:** S9, S10

**Files:**
- Modify: any files with remaining references to old fields

**Interfaces:**
- Consumes: all previous tasks
- Produces: fully working end-to-end flow

- [ ] **Step 1: Run full backend test suite**

Run: `uv run --project backend --extra dev pytest backend/tests -v`
Fix any failures from the model field changes.

- [ ] **Step 2: Run lint and typecheck**

Run: `uv run --project backend --extra dev ruff check backend/src backend/tests && uv run --project backend --extra dev mypy backend/src`
Fix any issues.

- [ ] **Step 3: Run frontend typecheck**

Run: `cd web && npx tsc -b`
Fix any type errors from the changed `SettingsSnapshot`.

- [ ] **Step 4: Run frontend build**

Run: `cd web && npm run build`
Expected: clean build with no errors.

- [ ] **Step 5: End-to-end smoke test**

Run: `docker compose up --build` (or `uv run --project backend uvicorn vsniper.api.main:app --reload --app-dir backend/src` + `cd web && npm run dev`)
Verify:
- AI Models page loads, can CRUD models
- Settings page loads, model selectors show registered models
- Saving settings with model IDs works
- Judge scanning still works with the new model resolution

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat: complete AI models registry — cleanup and integration fixes"
```
