# AI Models Registry — Design Spec

## [S1] Problem

AI provider and model configuration is scattered across the Settings page as free-text inputs with separate provider/model/effort dropdowns. There is no reusable model definition — the same model name must be typed in multiple places, and there's no validation that a referenced model actually exists. Cerebras was recently added but only as a fallback option, not as a first-class provider.

## [S2] Solution Overview

Introduce an `ai_models` database table as a model registry. Users define model entries (provider + model name + reasoning effort) on a dedicated "AI Models" page. All other model selections in the app reference these entries by ID via a reusable `ModelSelect` component. Provider-level config (API keys, base URLs) moves to the AI Models page.

## [S3] Data Model

### New table: `ai_models`

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `provider` | TEXT | NOT NULL | `"openai"` / `"cerebras"` / `"local"` |
| `model_name` | TEXT | NOT NULL | e.g. `"gpt-5.4-mini"`, `"gemma-4-31b"` |
| `reasoning_effort` | TEXT | NOT NULL | `"low"` / `"medium"` / `"high"` |
| `display_name` | TEXT | NOT NULL | Auto: `"{model_name} ({Provider})"` |
| `is_default` | INTEGER | DEFAULT 0 | 1 = shown first in selectors |
| `created_at` | INTEGER | NOT NULL | Epoch ms |
| `updated_at` | INTEGER | NOT NULL | Epoch ms |

### Simplified `AppSettingsState`

**Removed fields** (moved to model registry):
- `ai_judge_provider`, `ai_judge_model`, `local_judge_model`, `cerebras_judge_model`
- `ai_learn_model`, `ai_judge_reasoning_effort`, `ai_learn_reasoning_effort`
- `ai_observation_provider`, `local_observation_model`
- `ai_judge_allow_openai_fallback`, `ai_judge_fallback_provider`
- `ai_learn_image_detail`, `ai_judge_image_detail`

**New fields** (model ID references):
- `judge_model_id: str | None` — references `ai_models.id`
- `learn_model_id: str | None`
- `observation_model_id: str | None`
- `fallback_model_id: str | None` — `None` = no fallback

**Retained provider-level fields**:
- `ai_api_key` (OpenAI)
- `cerebras_api_key`, `cerebras_api_base_url`
- `local_vlm_base_url`

**Retained non-model fields**:
- `vlm_grid_size`, `vlm_pack_multiple_listing_images`, `vlm_judge_parallel_requests`
- `ai_judge_image_max_px`, `alert_threshold`
- All Vinted, Telegram, and other non-AI fields

## [S4] API Design

### Model CRUD

| Method | Path | Request | Response |
|--------|------|---------|----------|
| GET | `/api/ai-models` | — | `list[AiModelConfig]` |
| POST | `/api/ai-models` | `AiModelCreate` | `AiModelConfig` |
| PUT | `/api/ai-models/{id}` | `AiModelUpdate` | `AiModelConfig` |
| DELETE | `/api/ai-models/{id}` | — | 204 (fails if referenced) |

### Contracts

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
    model_name: str
    reasoning_effort: Literal["low", "medium", "high"]
    is_default: bool = False

class AiModelUpdate(BaseModel):
    model_name: str | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    is_default: bool | None = None
```

### Settings changes

`SettingsSnapshot` and `SettingsUpdate` replace per-model string fields with:
- `judge_model_id: str | None`
- `learn_model_id: str | None`
- `observation_model_id: str | None`
- `fallback_model_id: str | None`

`SettingsSnapshot` embeds `models: list[AiModelConfig]` for convenience.

## [S5] Frontend — AI Models Page

**Route**: `/ai-models` | **Icon**: `Cpu` | **Position**: sidebar between "AI Costs" and "Settings"

### Provider Credentials (top section)

Three collapsible cards:
- **OpenAI**: API key input (password field)
- **Cerebras**: API key (password) + base URL inputs
- **Local**: base URL input + "Test connection" button (moved from Settings)

### Model Registry (main section)

Table/card list of defined models:
- Each row: display name, provider badge, effort badge, edit/delete actions
- "Add model" button → inline form with provider dropdown, model name input, effort dropdown
- Display name auto-generated as `{model_name} ({Provider})`

## [S6] Frontend — ModelSelect Component

Reusable `<select>` component used on Settings, Searches, and any other page:
- Lists all registered models, formatted as `"gemma-4-31b (Cerebras) · low"`
- Optional `allowNone` prop → shows "None" as first option (for fallback)
- Returns model ID on change
- Disabled state when no models are registered

## [S7] Frontend — Settings Page Simplification

Settings page retains only:
1. **Vinted session** — cookie, region (unchanged)
2. **Judge** — alert threshold, VLM grid size, parallel requests, tile size, pack images, judge `ModelSelect`, fallback `ModelSelect` (with `allowNone`)
3. **Learn** — `ModelSelect` for learn model
4. **Observation** — `ModelSelect` for observation model
5. **Telegram** — bot token, chat ID, webhook URL, webhook secret (unchanged)

All free-text model inputs and provider/effort dropdowns removed.

## [S8] Backend Runtime Changes

`OpenAITasteClient` methods resolve model IDs to full config at call time:
1. Look up model entry from `ai_models` table by ID
2. Use `provider` to select API endpoint
3. Use `model_name` as the model parameter
4. Use `reasoning_effort` as the effort parameter

Fallback logic: if `fallback_model_id` is set, retry with that model on primary failure.

## [S9] Migration Strategy

1. Create `ai_models` table (Alembic migration)
2. Add new model ID columns to `app_settings_state`
3. Seed default model entries from current `.env` defaults:
   - `gpt-5.4-mini (OpenAI) · low` — default judge
   - `gemma-4-31b (Cerebras) · low` — default fallback
   - `gpt-5.5 (OpenAI) · medium` — default learn
   - `gemma4-12b-quality (Local) · low` — default local judge/observation
4. Set `judge_model_id`, `learn_model_id`, etc. to reference seeded models
5. Drop old columns in a later migration (or keep for backward compat during transition)

## [S10] Search Draft Integration

`TasteProfile` search drafts currently carry `ai_judge_provider` hints. After migration, drafts carry a `recommended_model_id` that references the model registry. When saving a search from a draft, the model ID is inherited.
